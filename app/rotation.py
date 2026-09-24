"""Core rotation state machine.

Deliberately has zero dependency on RingCentral, Signal, Flask, or
anything else with a network call — that's what makes it trivially unit
testable and safe to reason about in isolation. Everything else in the
app (the RingCentral driver, the Signal bot, the web dashboard) is a
thin adapter that calls into a RotationManager instance.

Persistence format (data/state.json):
{
  "order": ["fr_bugnini", "fr_youngtrad", "fr_martin"],   # ring order, id list
  "history": [
    {"timestamp": "...", "action": "rotate", "order_before": [...],
     "order_after": [...], "triggered_by": "signal:+1916...", "reason": "..."},
    ...
  ],
  "availability": {
    "fr_bugnini": {"manual_disabled": false, "day_off": "Tuesday",
                   "vacation": {"start": "2026-08-20", "end": "2026-08-27"},
                   "day_of_recollection": {"ordinal": 2}},
    ...
  },
  "pending_confirmations": {
    "fr_youngtrad": {"type": "day_off_confirm", "vacationing_priest": "fr_bugnini",
                 "current_day_off": null, "next_priest_id": "fr_martin"}
  },
  "automation_enabled": true,
  "last_applied_order": ["fr_bugnini", "fr_youngtrad"],
  "last_daily_run": "2026-08-12",
  "last_weekly_audit": "2026-08-10",
  "audit_in_progress": false,
  "audit_success_notices_remaining": 4,
  "audit_log": [
    {"timestamp": "...", "week_monday": "2026-08-17", "result": "pass", "problems": []}
  ]
}

Availability (manual disable / recurring day off / vacation / monthly
day of recollection) is operational state that changes often, so it
lives here rather than in config/priests.yaml (see config_store.py's
docstring on that split). pending_confirmations is how the Signal bot
resolves a plain-text reply ("KEEP", a weekday name, "Y"/"N", a menu
choice) to what's actually being asked, since Signal itself has no
message threading. automation_enabled is a single app-wide switch: when
false, day_off/vacation/day_of_recollection stop being evaluated (but
manual_disabled still is - see set_global_automation).
"""
from __future__ import annotations

import calendar
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.config_store import load_priests, save_priests
from app.localtime import (
    as_california_datetime,
    california_now,
    california_today,
    effective_ring_date,
    week_monday,
)


logger = logging.getLogger(__name__)


class RotationError(Exception):
    pass


WEEKDAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
VALID_WEEKDAYS = set(WEEKDAY_ORDER)
WEDNESDAY_INDEX = 2  # date.weekday(): Monday=0 ... Sunday=6




def parse_weekday_input(text: str) -> str | None:
    """Parse a flexible weekday reference ("Mondays", "Mon", "M",
    "monday"...) into a canonical name from WEEKDAY_ORDER, the sentinel
    "AMBIGUOUS" for a bare prefix matching more than one day (only "T"
    and "S" are ambiguous among the seven full names), or None if it
    doesn't match anything."""
    stripped = text.strip()
    if not stripped:
        return None
    normalized = stripped.capitalize()
    if len(normalized) > 1 and normalized.endswith("s"):
        normalized = normalized[:-1]
    if normalized in VALID_WEEKDAYS:
        return normalized
    matches = [day for day in WEEKDAY_ORDER if day.startswith(normalized)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return "AMBIGUOUS"
    return None


def nth_weekday_of_month(year: int, month: int, weekday_index: int, ordinal: int) -> date | None:
    """The date of the ordinal'th (1-5) occurrence of weekday_index
    (Monday=0 ... Sunday=6) in the given year/month, or None if that
    month doesn't have that many occurrences (e.g. no 5th Wednesday)."""
    first_of_month = date(year, month, 1)
    first_weekday_offset = (weekday_index - first_of_month.weekday()) % 7
    day = 1 + first_weekday_offset + (ordinal - 1) * 7
    days_in_month = calendar.monthrange(year, month)[1]
    if day > days_in_month:
        return None
    return date(year, month, day)


def next_weekday_on_or_after(weekday: str, after: date) -> date:
    """Next calendar date on or after `after` whose weekday name matches."""
    target = WEEKDAY_ORDER.index(weekday)
    return after + timedelta(days=(target - after.weekday()) % 7)


def next_recollection_date(ordinal: int, after: date) -> date:
    """The next date (>= after) that's the ordinal'th Wednesday of its
    month, walking forward month by month if a given month doesn't have
    that many Wednesdays. Used only for the confirmation message text -
    the actual availability check is a direct same-day computation."""
    year, month = after.year, after.month
    while True:
        candidate = nth_weekday_of_month(year, month, WEDNESDAY_INDEX, ordinal)
        if candidate and candidate >= after:
            return candidate
        month += 1
        if month > 12:
            month = 1
            year += 1


def _read_json_object(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        state = json.load(f)
    if not isinstance(state, dict):
        raise ValueError(f"{path.name} does not hold a JSON object")
    return state


# Removed with visit counting (24 Sep 2026): state keys and history entry
# types that only carried counts. Deleted from state.json on first load.
_REMOVED_COUNT_KEYS = ("call_counts", "people_counts", "rounds", "rotate_at", "anointing_log")
_REMOVED_COUNT_ACTIONS = {
    "visits_added",
    "call_count_reported",
    "call_recorded",
    "set_round_progress",
    "reset_year_totals",
}


@dataclass
class RotationManager:
    config_path: Path
    state_path: Path

    _order: list[str] = field(default_factory=list, init=False)
    _history: list[dict[str, Any]] = field(default_factory=list, init=False)
    _availability: dict[str, dict[str, Any]] = field(default_factory=dict, init=False)
    _pending_confirmations: dict[str, dict[str, Any]] = field(default_factory=dict, init=False)
    _automation_enabled: bool = field(default=True, init=False)
    _order_66_executed: bool = field(default=False, init=False)
    _last_notified_lead_id: str | None = field(default=None, init=False)
    _last_applied_order: list[str] = field(default_factory=list, init=False)
    _last_daily_run: str | None = field(default=None, init=False)
    _last_weekly_audit: str | None = field(default=None, init=False)
    _audit_in_progress: bool = field(default=False, init=False)
    _audit_success_notices_remaining: int = field(default=4, init=False)
    _audit_restore: dict[str, Any] | None = field(default=None, init=False)
    _audit_log: list[dict[str, Any]] = field(default_factory=list, init=False)
    _week_day_off_overrides: dict[str, dict[str, Any]] = field(default_factory=dict, init=False)
    _recollection_skips: dict[str, list[str]] = field(default_factory=dict, init=False)
    _cover_prompts_sent: dict[str, str] = field(default_factory=dict, init=False)
    _failsafe_active: bool = field(default=False, init=False)
    _signal_down_alerted: bool = field(default=False, init=False)
    # Set when state.json was unreadable and the previous save was used
    # instead: the name the damaged file was kept under (see _read_state).
    recovered_from_damage: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def reload(self) -> None:
        """Re-read state.json/priests.yaml from disk. The web dashboard
        and the Signal bot each hold their own RotationManager instance
        in the same process (see app/main.py vs app/web/__init__.py) -
        without this, a change made through one wouldn't be visible to
        the other until the container restarted. Cheap enough (small
        local files) to call on every poll cycle / request."""
        self._load()

    # ---------- persistence ----------

    def _load(self) -> None:
        priests = load_priests(self.config_path)
        active_ids = [p["id"] for p in priests if p.get("active", True)]

        state = self._read_state()
        if state is not None:
            saved_order = state.get("order", [])
            # Preserve saved order for ids that still exist and are
            # active; append any newly-active ids not yet in the order;
            # drop ids that were removed/deactivated.
            self._order = [pid for pid in saved_order if pid in active_ids]
            for pid in active_ids:
                if pid not in self._order:
                    self._order.append(pid)
            # Visit counting was removed on 24 Sep 2026 (rotation is ROTATE
            # only). Drop the old count data and count-only history entries
            # the first time an older state.json is loaded.
            purge_counts = any(key in state for key in _REMOVED_COUNT_KEYS)
            self._history = [
                h for h in state.get("history", []) if h.get("action") not in _REMOVED_COUNT_ACTIONS
            ]
            purge_counts = purge_counts or len(self._history) != len(state.get("history", []))
            self._availability = state.get("availability", {})
            self._pending_confirmations = state.get("pending_confirmations", {})
            self._automation_enabled = state.get("automation_enabled", True)
            self._order_66_executed = state.get("order_66_executed", False)
            if "last_notified_lead_id" in state:
                self._last_notified_lead_id = state["last_notified_lead_id"]
            else:
                # First load after this feature was added to an existing
                # deployment - seed silently from the current effective
                # lead rather than firing a false "lead changed" alert
                # the first time sync_lead_notification_state() runs.
                effective = self.effective_order()
                self._last_notified_lead_id = effective[0]["id"] if effective else None
            if "last_applied_order" in state:
                self._last_applied_order = list(state["last_applied_order"])
            else:
                # Same silent seed: don't treat a first boot as "the ring
                # just changed" and PUT the live line unprompted.
                self._last_applied_order = [p["id"] for p in self.effective_order()]
            self._last_daily_run = state.get("last_daily_run")
            self._last_weekly_audit = state.get("last_weekly_audit")
            self._audit_in_progress = bool(state.get("audit_in_progress", False))
            if "audit_success_notices_remaining" in state:
                self._audit_success_notices_remaining = int(state["audit_success_notices_remaining"])
            else:
                # Existing deploy just got this feature: Martin hears the
                # next four successful Monday audits, then success is silent.
                self._audit_success_notices_remaining = 4
            self._audit_restore = state.get("audit_restore")
            self._audit_log = list(state.get("audit_log", []))
            self._week_day_off_overrides = state.get("week_day_off_overrides", {})
            self._recollection_skips = {
                key: list(ids) for key, ids in (state.get("recollection_skips") or {}).items()
            }
            self._cover_prompts_sent = state.get("cover_prompts_sent", {})
            self._failsafe_active = bool(state.get("failsafe_active", False))
            self._signal_down_alerted = bool(state.get("signal_down_alerted", False))
            if purge_counts:
                self._save()
        else:
            self._order = active_ids
            self._history = []
            self._availability = {}
            self._pending_confirmations = {}
            self._automation_enabled = True
            self._order_66_executed = False
            self._last_notified_lead_id = active_ids[0] if active_ids else None
            self._last_applied_order = list(active_ids)
            self._last_daily_run = None
            self._last_weekly_audit = None
            self._audit_in_progress = False
            self._audit_success_notices_remaining = 4
            self._audit_restore = None
            self._audit_log = []
            self._week_day_off_overrides = {}
            self._recollection_skips = {}
            self._cover_prompts_sent = {}
            self._failsafe_active = False
            self._signal_down_alerted = False
            self._save()

    def _save(self) -> None:
        state = {
            "order": self._order,
            "history": self._history,
            "availability": self._availability,
            "pending_confirmations": self._pending_confirmations,
            "automation_enabled": self._automation_enabled,
            "order_66_executed": self._order_66_executed,
            "last_notified_lead_id": self._last_notified_lead_id,
            "last_applied_order": self._last_applied_order,
            "last_daily_run": self._last_daily_run,
            "last_weekly_audit": self._last_weekly_audit,
            "audit_in_progress": self._audit_in_progress,
            "audit_success_notices_remaining": self._audit_success_notices_remaining,
            "audit_restore": self._audit_restore,
            "audit_log": self._audit_log,
            "week_day_off_overrides": self._week_day_off_overrides,
            "recollection_skips": self._recollection_skips,
            "cover_prompts_sent": self._cover_prompts_sent,
            "failsafe_active": self._failsafe_active,
            "signal_down_alerted": self._signal_down_alerted,
        }
        self._write_state(state)

    def _backup_path(self) -> Path:
        return self.state_path.with_suffix(".json.bak")

    def _read_state(self) -> dict[str, Any] | None:
        """The saved state, or None on a first run (no files yet).

        If a power cut left state.json empty or half-written, fall back to
        state.json.bak (the version before the last save), keep the damaged
        file for inspection, and write the recovered state back. At most
        the last change is lost. Raises only when neither copy is readable:
        starting over from a blank state could ring the wrong priests."""
        backup = self._backup_path()
        if not self.state_path.exists() and not backup.exists():
            return None
        try:
            return _read_json_object(self.state_path)
        except (OSError, ValueError) as exc:
            primary_error = exc
        try:
            state = _read_json_object(backup)
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"{self.state_path.name} is unreadable ({primary_error}) and so is "
                f"{backup.name} ({exc}); restore data/ from the nightly backup (ops/RESTORE.md)"
            ) from primary_error
        damaged = None
        if self.state_path.exists():
            damaged = self.state_path.with_name(
                f"{self.state_path.name}.damaged-{datetime.now():%Y%m%d-%H%M%S}"
            )
            self.state_path.replace(damaged)
        self.recovered_from_damage = damaged.name if damaged else f"{self.state_path.name} (missing)"
        logger.error(
            "%s was unreadable (%s); recovered from %s. Damaged file kept as %s.",
            self.state_path.name, primary_error, backup.name, self.recovered_from_damage,
        )
        # The backup is the good copy here: don't replace it with the damaged file.
        self._write_state(state, rotate_backup=False)
        return state

    def _write_state(self, state: dict[str, Any], rotate_backup: bool = True) -> None:
        """Write state.json so a power cut at any moment leaves a readable
        state.json, with the previous version kept as state.json.bak."""
        tmp_path = self.state_path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())  # data on disk before the rename below makes it live
        if rotate_backup and self.state_path.exists():
            # Hard link, so state.json.bak becomes the current (soon previous)
            # file without copying it. Best effort: a missed backup must never
            # block saving the state itself.
            bak_tmp = self.state_path.with_suffix(".json.bak.tmp")
            try:
                bak_tmp.unlink(missing_ok=True)
                os.link(self.state_path, bak_tmp)
                bak_tmp.replace(self._backup_path())
            except OSError:
                logger.warning("Could not update %s", self._backup_path().name, exc_info=True)
        tmp_path.replace(self.state_path)  # atomic on POSIX
        dir_fd = os.open(self.state_path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)  # make the rename itself survive a power cut
        finally:
            os.close(dir_fd)

    def _priests_by_id(self) -> dict[str, dict[str, Any]]:
        return {p["id"]: p for p in load_priests(self.config_path)}

    def _log(self, action: str, triggered_by: str, reason: str = "", **extra: Any) -> None:
        entry = {
            "timestamp": california_now().isoformat(),
            "action": action,
            "triggered_by": triggered_by,
            "reason": reason,
            **extra,
        }
        self._history.append(entry)

    # ---------- availability ----------

    @property
    def automation_enabled(self) -> bool:
        return self._automation_enabled

    @staticmethod
    def _default_availability() -> dict[str, Any]:
        return {
            "manual_disabled": False,
            "day_off": None,
            "vacation": None,
            "day_of_recollection": None,
            "notifications_muted": False,
        }

    def _get_availability(self, priest_id: str) -> dict[str, Any]:
        return self._availability.get(priest_id, self._default_availability())

    @staticmethod
    def _is_recollection_active(avail: dict[str, Any], at: datetime) -> bool:
        recollection = avail.get("day_of_recollection")
        if not recollection:
            return False
        ring_day = effective_ring_date(at)
        if ring_day.weekday() != WEDNESDAY_INDEX:
            return False
        return (ring_day.day - 1) // 7 + 1 == recollection["ordinal"]

    @staticmethod
    def _on_vacation(avail: dict[str, Any], ring_day: date) -> bool:
        vacation = avail.get("vacation")
        if not vacation:
            return False
        start = date.fromisoformat(vacation["start"])
        end = date.fromisoformat(vacation["end"])
        return start <= ring_day <= end

    def _hard_unavailable(self, avail: dict[str, Any], at: datetime, automation_enabled: bool) -> bool:
        """Away for a reason that cannot be skipped: manual disable, or vacation."""
        if avail.get("manual_disabled"):
            return True
        if not automation_enabled:
            return False
        return self._on_vacation(avail, effective_ring_date(at))

    def _peer_on_vacation(self, priest_id: str, ring_day: date) -> bool:
        return any(
            pid != priest_id and self._on_vacation(self._get_availability(pid), ring_day) for pid in self._order
        )

    def _anyone_else_hard_available(self, priest_id: str, at: datetime) -> bool:
        return any(
            pid != priest_id
            and not self._hard_unavailable(self._get_availability(pid), at, self._automation_enabled)
            for pid in self._order
        )

    def _day_off_applies(self, priest_id: str, avail: dict[str, Any], ring_day: date) -> bool:
        """Whether this priest's day off (or a one-week move) takes them
        off the ring. During someone else's vacation, day offs are skipped
        unless they explicitly moved theirs that week. No reply = everyone
        remaining stays ON."""
        override = self._week_day_off_overrides.get(week_monday(ring_day).isoformat(), {}).get(priest_id)
        if override and override.get("action") == "move":
            return ring_day.strftime("%A") == override.get("weekday")
        if override and override.get("action") == "skip":
            return False
        if self._peer_on_vacation(priest_id, ring_day):
            return False
        return bool(avail.get("day_off") and ring_day.strftime("%A") == avail["day_off"])

    def _is_available_given(
        self, avail: dict[str, Any], on_date: date | datetime, automation_enabled: bool = True, priest_id: str | None = None
    ) -> bool:
        at = as_california_datetime(on_date)
        ring_day = effective_ring_date(at)
        if self._hard_unavailable(avail, at, automation_enabled):
            return False
        if not automation_enabled:
            return True
        soft_off = self._is_recollection_active(avail, at)
        if priest_id and soft_off and self._is_recollection_skipped(priest_id, ring_day):
            soft_off = False
        if priest_id:
            soft_off = soft_off or self._day_off_applies(priest_id, avail, ring_day)
        elif avail.get("day_off") and ring_day.strftime("%A") == avail["day_off"]:
            soft_off = True
        if not soft_off:
            return True
        # Last person on the line: skip day off / recollection so it always rings.
        if priest_id and not self._anyone_else_hard_available(priest_id, at):
            return True
        if priest_id is None and not any(
            not self._hard_unavailable(self._get_availability(pid), at, automation_enabled)
            for pid in self._order
        ):
            return True
        return False

    def is_available(self, priest_id: str, on_date: date | datetime | None = None) -> bool:
        return self._is_available_given(
            self._get_availability(priest_id),
            california_now() if on_date is None else on_date,
            self._automation_enabled,
            priest_id=priest_id,
        )

    def effective_order(self, on_date: date | datetime | None = None) -> list[dict[str, Any]]:
        """current_order() filtered to priests available at on_date
        (defaults to now, California). Day off, recollection, and
        vacation all start at 8pm the evening before and end at 8pm
        the listed day. This is what gets pushed to RingCentral."""
        at = california_now() if on_date is None else on_date
        return [p for p in self.current_order() if self.is_available(p["id"], at)]

    def sync_lead_notification_state(self) -> str | None:
        """Returns the new effective lead's id if it differs from the
        last time this was called (and records the new value), else
        None. This is what lets the bot notice an on-call change from
        ANY cause - not just an explicit rotate(), but also an
        automatic availability shift (a day off/vacation/recollection
        starting or ending, or the global automation toggle) - without
        needing to instrument every place availability can change.
        rotate() pre-syncs this itself so an
        explicit rotation's own notification isn't duplicated here."""
        current = self.effective_order()
        new_lead_id = current[0]["id"] if current else None
        if new_lead_id == self._last_notified_lead_id:
            return None
        self._last_notified_lead_id = new_lead_id
        self._save()
        return new_lead_id

    @property
    def last_notified_lead_id(self) -> str | None:
        return self._last_notified_lead_id

    @property
    def last_applied_order(self) -> list[str]:
        return list(self._last_applied_order)

    def mark_applied_order(self, priest_ids: list[str]) -> None:
        """Record the effective ring that was successfully pushed to
        RingCentral so the next poll only PUTs if it actually changed.
        Not updated when a push fails — that is what makes a failed
        automatic update retry on the next cycle."""
        self._last_applied_order = list(priest_ids)
        self._save()

    @property
    def last_daily_run(self) -> str | None:
        return self._last_daily_run

    def mark_daily_run(self, on_date: date) -> None:
        self._last_daily_run = on_date.isoformat()
        self._save()

    @property
    def last_weekly_audit(self) -> str | None:
        return self._last_weekly_audit

    def mark_weekly_audit(self, on_date: date) -> None:
        """Record the Monday this week's self-audit already ran."""
        self._last_weekly_audit = on_date.isoformat()
        self._save()

    @property
    def audit_in_progress(self) -> bool:
        return self._audit_in_progress

    def set_audit_in_progress(self, active: bool) -> None:
        self._audit_in_progress = active
        if not active:
            self._audit_restore = None
        self._save()

    def begin_weekly_audit(self) -> dict[str, Any]:
        """Persist a restore point so a crash mid-audit can roll back."""
        snapshot = self.audit_snapshot()
        self._audit_restore = snapshot
        self._audit_in_progress = True
        self._save()
        return snapshot

    def recover_interrupted_audit(self) -> bool:
        """Roll back a crash mid-audit. Returns True if a restore ran."""
        snapshot = self._audit_restore
        if snapshot is None and not self._audit_in_progress:
            return False
        if snapshot is not None:
            self.restore_audit_snapshot(snapshot)
        self._audit_in_progress = False
        self._audit_restore = None
        self._save()
        return True

    @property
    def audit_success_notices_remaining(self) -> int:
        return self._audit_success_notices_remaining

    def set_audit_success_notices_remaining(self, count: int) -> None:
        self._audit_success_notices_remaining = max(0, int(count))
        self._save()

    def take_audit_success_notice(self) -> bool:
        """True if Martin should still hear this week's successful audit."""
        if self._audit_success_notices_remaining <= 0:
            return False
        self._audit_success_notices_remaining -= 1
        self._save()
        return True

    def audit_snapshot(self) -> dict[str, Any]:
        """Copy of the bits the weekly audit may temporarily change."""
        return {
            "order": list(self._order),
            "automation_enabled": self._automation_enabled,
            "failsafe_active": self._failsafe_active,
            "last_notified_lead_id": self._last_notified_lead_id,
            "last_applied_order": list(self._last_applied_order),
            "availability": json.loads(json.dumps(self._availability)),
        }

    def restore_audit_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._order = list(snapshot["order"])
        self._automation_enabled = bool(snapshot["automation_enabled"])
        self._failsafe_active = bool(snapshot["failsafe_active"])
        self._last_notified_lead_id = snapshot["last_notified_lead_id"]
        self._last_applied_order = list(snapshot["last_applied_order"])
        self._availability = json.loads(json.dumps(snapshot["availability"]))
        self._save()

    def record_audit_result(
        self,
        result: str,
        problems: list[str],
        week_start: date,
        checks: list[str] | None = None,
    ) -> None:
        self._audit_log.append(
            {
                "timestamp": california_now().isoformat(),
                "week_monday": week_start.isoformat(),
                "result": result,
                "problems": list(problems),
                "checks": list(checks or []),
            }
        )
        self._prune_audit_log()
        self._log(
            "weekly_audit",
            triggered_by="system-audit",
            reason="; ".join(problems) if problems else "passed",
            result=result,
            week_monday=week_start.isoformat(),
        )
        self._save()

    def _audit_entry_time(self, entry: dict[str, Any]) -> datetime | None:
        raw = entry.get("week_monday") or entry.get("timestamp")
        if not raw:
            return None
        try:
            text = str(raw)
            if "T" in text:
                return as_california_datetime(datetime.fromisoformat(text))
            return as_california_datetime(date.fromisoformat(text[:10]))
        except (TypeError, ValueError):
            return None

    def _prune_audit_log(self) -> None:
        cutoff = california_now() - timedelta(days=93)
        kept: list[dict[str, Any]] = []
        for entry in self._audit_log:
            when = self._audit_entry_time(entry)
            if when is None or when >= cutoff:
                kept.append(entry)
        self._audit_log = kept

    def audit_log(self, months: int = 3) -> list[dict[str, Any]]:
        """Newest-first audit results from the last `months` months."""
        cutoff = california_now() - timedelta(days=31 * months)
        rows = []
        for entry in self._audit_log:
            when = self._audit_entry_time(entry)
            if when is None or when >= cutoff:
                rows.append(entry)
        return list(reversed(rows))

    def _validate_would_keep_coverage(
        self, priest_id: str, new_avail: dict[str, Any], on_date: date | datetime | None = None
    ) -> None:
        """Raise if applying new_avail for priest_id would leave zero
        priests available on on_date. This is the "at least one active
        priest at all times" floor."""
        at = california_now() if on_date is None else on_date
        for pid in self._order:
            avail = new_avail if pid == priest_id else self._get_availability(pid)
            if self._is_available_given(avail, at, self._automation_enabled, priest_id=pid):
                return
        label = as_california_datetime(at).date().isoformat()
        raise RotationError(f"This change would leave zero priests available on {label}.")

    def _would_automation_enable_zero_coverage(self, on_date: date | datetime) -> bool:
        return not any(
            self._is_available_given(
                self._get_availability(pid), on_date, automation_enabled=True, priest_id=pid
            )
            for pid in self._order
        )

    def set_week_day_off_override(
        self, week_start: date, priest_id: str, action: str, weekday: str | None = None, triggered_by: str = ""
    ) -> None:
        """One-week day-off exception: skip, or move to another weekday.

        A move that would leave the line empty that day is rejected."""
        if priest_id not in self._order:
            raise RotationError(f"priest id '{priest_id}' not found in active order")
        if action not in ("skip", "move"):
            raise RotationError(f"unknown week day-off action '{action}'")
        if action == "move":
            if weekday not in VALID_WEEKDAYS:
                raise RotationError(f"'{weekday}' is not a valid day of the week")
            offset = WEEKDAY_ORDER.index(weekday)
            target_day = week_start + timedelta(days=offset)
            probe = datetime(target_day.year, target_day.month, target_day.day, 12, 0)
            # Temporarily apply the move to validate coverage that day.
            key = week_start.isoformat()
            previous = self._week_day_off_overrides.get(key, {}).get(priest_id)
            week = dict(self._week_day_off_overrides.get(key, {}))
            week[priest_id] = {"action": "move", "weekday": weekday}
            self._week_day_off_overrides[key] = week
            try:
                if not self._anyone_else_hard_available(priest_id, as_california_datetime(probe)):
                    raise RotationError(
                        f"Moving your day off to {weekday} would leave no one on the line that day."
                    )
            except RotationError:
                if previous is None:
                    self._week_day_off_overrides[key].pop(priest_id, None)
                else:
                    self._week_day_off_overrides[key][priest_id] = previous
                raise
        else:
            key = week_start.isoformat()
            week = dict(self._week_day_off_overrides.get(key, {}))
            week[priest_id] = {"action": "skip"}
            self._week_day_off_overrides[key] = week
        self._log(
            "set_week_day_off_override",
            triggered_by=triggered_by,
            priest_id=priest_id,
            week_start=week_start.isoformat(),
            override_action=action,
            weekday=weekday,
        )
        self._save()

    def skip_days_off_until(
        self, priest_id: str, from_week: date, until_date: date, triggered_by: str
    ) -> None:
        """Skip this priest's day off each week from from_week through until_date."""
        week = week_monday(from_week)
        last = week_monday(until_date)
        while week <= last:
            self.set_week_day_off_override(week, priest_id, "skip", triggered_by=triggered_by)
            week += timedelta(days=7)

    def _is_recollection_skipped(self, priest_id: str, on_date: date) -> bool:
        return priest_id in (self._recollection_skips.get(on_date.isoformat()) or [])

    def skip_recollection(self, priest_id: str, on_date: date, triggered_by: str, reason: str = "") -> None:
        if priest_id not in self._order:
            raise RotationError(f"priest id '{priest_id}' not found in active order")
        key = on_date.isoformat()
        ids = list(self._recollection_skips.get(key, []))
        if priest_id not in ids:
            ids.append(priest_id)
        self._recollection_skips[key] = ids
        self._log(
            "skip_recollection",
            triggered_by=triggered_by,
            reason=reason,
            priest_id=priest_id,
            on_date=key,
        )
        self._save()

    def scheduled_skip_targets(self, priest_id: str, after: date | None = None) -> list[dict[str, Any]]:
        """Next skippable day-off and recollection occurrences."""
        after = after or effective_ring_date()
        avail = self._get_availability(priest_id)
        items: list[dict[str, Any]] = []

        day_off = avail.get("day_off")
        if day_off:
            candidate = next_weekday_on_or_after(day_off, after)
            for _ in range(8):
                week = week_monday(candidate)
                override = self._week_day_off_overrides.get(week.isoformat(), {}).get(priest_id)
                if not (override and override.get("action") == "skip"):
                    items.append(
                        {
                            "kind": "day_off",
                            "weekday": day_off,
                            "when": candidate.isoformat(),
                            "week_start": week.isoformat(),
                            "label": f"Day off every {day_off} (next {candidate.strftime('%m/%d')})",
                        }
                    )
                    break
                candidate = candidate + timedelta(days=7)

        recollection = avail.get("day_of_recollection") or {}
        ordinal = recollection.get("ordinal")
        if ordinal:
            candidate = next_recollection_date(int(ordinal), after)
            for _ in range(6):
                if not self._is_recollection_skipped(priest_id, candidate):
                    suffix = {1: "st", 2: "nd", 3: "rd"}.get(int(ordinal), "th")
                    items.append(
                        {
                            "kind": "recollection",
                            "ordinal": int(ordinal),
                            "when": candidate.isoformat(),
                            "label": (
                                f"Recollection: {ordinal}{suffix} Wednesday "
                                f"(next {candidate.strftime('%m/%d')})"
                            ),
                        }
                    )
                    break
                candidate = next_recollection_date(int(ordinal), candidate + timedelta(days=1))
        return items

    def skip_scheduled_absence(
        self, priest_id: str, item: dict[str, Any], triggered_by: str, reason: str = ""
    ) -> dict[str, Any]:
        """Skip one upcoming automatic day off or recollection. Returns the item."""
        kind = item.get("kind")
        if kind == "day_off":
            week_start = date.fromisoformat(item["week_start"])
            self.set_week_day_off_override(week_start, priest_id, "skip", triggered_by=triggered_by)
        elif kind == "recollection":
            self.skip_recollection(
                priest_id, date.fromisoformat(item["when"]), triggered_by=triggered_by, reason=reason
            )
        else:
            raise RotationError(f"unknown skip kind '{kind}'")
        return item

    def mark_cover_prompt_sent(self, week_start: date, kind: str) -> bool:
        """Record that a cover prompt went out for this week. Returns
        False if that week+kind was already sent."""
        key = f"{week_start.isoformat()}:{kind}"
        if key in self._cover_prompts_sent:
            return False
        self._cover_prompts_sent[key] = california_now().isoformat()
        self._save()
        return True

    def cover_prompt_targets(self, week_start: date) -> list[dict[str, Any]]:
        """Priests who are not on vacation that week and have a day off
        to skip or move, plus who is away."""
        week_end = week_start + timedelta(days=6)
        away = []
        staying = []
        for pid in self._order:
            avail = self._get_availability(pid)
            vacation = avail.get("vacation")
            on_vacation_this_week = False
            if vacation:
                start = date.fromisoformat(vacation["start"])
                end = date.fromisoformat(vacation["end"])
                on_vacation_this_week = not (end < week_start or start > week_end)
            record = next((p for p in self.current_order() if p["id"] == pid), {"id": pid, "name": pid})
            if on_vacation_this_week:
                away.append(record)
            else:
                staying.append({**record, "day_off": avail.get("day_off")})
        if not away:
            return []
        away_names = [p["name"] for p in away]
        vacation_end = max(
            date.fromisoformat(self._get_availability(p["id"])["vacation"]["end"]) for p in away
        )
        targets = []
        for p in staying:
            if not p.get("day_off"):
                continue
            targets.append(
                {
                    "id": p["id"],
                    "name": p["name"],
                    "cell_number": p.get("cell_number"),
                    "day_off": p["day_off"],
                    "away_names": away_names,
                    "vacation_end": vacation_end.isoformat(),
                    "week_start": week_start.isoformat(),
                }
            )
        return targets

    def upcoming_cover_weeks(self, ring_day: date) -> list[date]:
        """Weeks that should get a 2-days-ahead cover prompt today."""
        weeks = []
        seen: set[date] = set()
        for pid in self._order:
            vacation = self._get_availability(pid).get("vacation")
            if not vacation:
                continue
            start = date.fromisoformat(vacation["start"])
            notice_day = start - timedelta(days=2)
            if notice_day <= ring_day < start:
                monday = week_monday(start)
                if monday not in seen:
                    seen.add(monday)
                    weeks.append(monday)
        return weeks

    def sunday_followup_week(self, now: datetime) -> date | None:
        """If it's Sunday 3pm and an absence continues into next week,
        return that next week's Monday."""
        at = as_california_datetime(now)
        if at.strftime("%A") != "Sunday" or at.hour != 15:
            return None
        next_monday = week_monday(at.date()) + timedelta(days=7)
        for pid in self._order:
            vacation = self._get_availability(pid).get("vacation")
            if not vacation:
                continue
            start = date.fromisoformat(vacation["start"])
            end = date.fromisoformat(vacation["end"])
            if start < at.date() and end >= next_monday:
                return next_monday
        return None

    def set_manual_disable(self, priest_id: str, disabled: bool, triggered_by: str, reason: str = "") -> None:
        if priest_id not in self._order:
            raise RotationError(f"priest id '{priest_id}' not found in active order")
        new_avail = {**self._get_availability(priest_id), "manual_disabled": disabled}
        if disabled:
            self._validate_would_keep_coverage(priest_id, new_avail)
        self._availability[priest_id] = new_avail
        self._log("set_manual_disable", triggered_by=triggered_by, reason=reason, priest_id=priest_id, disabled=disabled)
        self._save()

    def set_day_off(self, priest_id: str, weekday: str | None, triggered_by: str, reason: str = "") -> None:
        if priest_id not in self._order:
            raise RotationError(f"priest id '{priest_id}' not found in active order")
        if weekday is not None and weekday not in VALID_WEEKDAYS:
            raise RotationError(f"'{weekday}' is not a valid day of the week")
        new_avail = {**self._get_availability(priest_id), "day_off": weekday}
        if weekday is not None and effective_ring_date(california_now()).strftime("%A") == weekday:
            self._validate_would_keep_coverage(priest_id, new_avail)
        self._availability[priest_id] = new_avail
        self._log("set_day_off", triggered_by=triggered_by, reason=reason, priest_id=priest_id, day_off=weekday)
        self._save()

    def set_vacation(
        self, priest_id: str, start: date | None, end: date | None, triggered_by: str, reason: str = ""
    ) -> None:
        if priest_id not in self._order:
            raise RotationError(f"priest id '{priest_id}' not found in active order")
        if (start is None) != (end is None):
            raise RotationError("vacation start and end must be set (or cleared) together")
        if start and end and start > end:
            raise RotationError("vacation start must be on or before end")

        vacation = {"start": start.isoformat(), "end": end.isoformat()} if start else None
        new_avail = {**self._get_availability(priest_id), "vacation": vacation}
        ring_day = effective_ring_date(california_now())
        if vacation and start <= ring_day <= end:
            self._validate_would_keep_coverage(priest_id, new_avail)
        self._availability[priest_id] = new_avail
        self._log(
            "set_vacation",
            triggered_by=triggered_by,
            reason=reason,
            priest_id=priest_id,
            vacation_start=vacation["start"] if vacation else None,
            vacation_end=vacation["end"] if vacation else None,
        )
        self._save()

    def set_day_of_recollection(
        self, priest_id: str, ordinal: int | None, triggered_by: str, reason: str = ""
    ) -> None:
        if priest_id not in self._order:
            raise RotationError(f"priest id '{priest_id}' not found in active order")
        if ordinal is not None and not (1 <= ordinal <= 5):
            raise RotationError("day of recollection ordinal must be between 1 and 5")

        recollection = {"ordinal": ordinal} if ordinal is not None else None
        new_avail = {**self._get_availability(priest_id), "day_of_recollection": recollection}
        if recollection and RotationManager._is_recollection_active(new_avail, california_now()):
            self._validate_would_keep_coverage(priest_id, new_avail)
        self._availability[priest_id] = new_avail
        self._log(
            "set_day_of_recollection", triggered_by=triggered_by, reason=reason, priest_id=priest_id, ordinal=ordinal
        )
        self._save()

    def set_global_automation(self, enabled: bool, triggered_by: str, reason: str = "") -> None:
        """The global Disable/Enable toggle. Turning OFF is a one-time
        action: clears manual_disabled for anyone currently set (so
        everyone's reachable immediately), then suspends automatic
        (day off / vacation / day of recollection) evaluation going
        forward - stored schedules aren't touched, just ignored while
        off. Turning back ON resumes them exactly as configured, but is
        rejected if doing so would leave zero priests available today."""
        if enabled and not self._automation_enabled and self._would_automation_enable_zero_coverage(california_now()):
            raise RotationError(
                f"Re-enabling automatic scheduling would leave zero priests available now "
                f"({california_now().isoformat()})."
            )

        cleared: list[str] = []
        if not enabled:
            for pid, avail in self._availability.items():
                if avail.get("manual_disabled"):
                    self._availability[pid] = {**avail, "manual_disabled": False}
                    cleared.append(pid)

        self._automation_enabled = enabled
        if enabled:
            self._failsafe_active = False
        self._log(
            "set_global_automation",
            triggered_by=triggered_by,
            reason=reason,
            enabled=enabled,
            cleared_manual_disables=cleared,
        )
        self._save()

    @property
    def failsafe_active(self) -> bool:
        return self._failsafe_active

    def set_failsafe_active(self, active: bool, reason: str = "") -> None:
        self._failsafe_active = active
        self._log("set_failsafe", triggered_by="system-failsafe", reason=reason, active=active)
        self._save()

    @property
    def signal_down_alerted(self) -> bool:
        return self._signal_down_alerted

    def set_signal_down_alerted(self, alerted: bool) -> None:
        if self._signal_down_alerted == alerted:
            return
        self._signal_down_alerted = alerted
        self._save()

    def set_notifications_muted(self, priest_id: str, muted: bool, triggered_by: str, reason: str = "") -> None:
        """Mute/unmute routine broadcast notifications (rotation
        announcements, vacation notices, visit-count updates) to this
        priest. Distinct from manual_disabled: a muted priest still
        participates in the ring rotation and can still text the bot
        directly and get replies - this only suppresses unsolicited
        broadcasts. System failure alerts (Notifier.alert) are
        unaffected by this - see notifiable_numbers() below."""
        if priest_id not in self._order:
            raise RotationError(f"priest id '{priest_id}' not found in active order")
        new_avail = {**self._get_availability(priest_id), "notifications_muted": muted}
        self._availability[priest_id] = new_avail
        self._log(
            "set_notifications_muted", triggered_by=triggered_by, reason=reason, priest_id=priest_id, muted=muted
        )
        self._save()

    def notifiable_numbers(self, priests: list[dict[str, Any]] | None = None) -> list[str]:
        """Cell numbers for routine broadcast notifications, excluding
        muted priests. Defaults to everyone in current_order(); pass a
        filtered list (e.g. excluding the priest who triggered the
        notification) to narrow it further."""
        if self._audit_in_progress:
            return []
        priests = priests if priests is not None else self.current_order()
        return [p["cell_number"] for p in priests if p.get("cell_number") and not p.get("notifications_muted")]

    def execute_order_66(self, triggering_priest_id: str, triggered_by: str) -> dict[str, Any]:
        """Toggle broadcast notifications for every priest except the
        sender. If any of them are muted, unmute them all; if they are
        all unmuted, mute them all. Does not send any Signal texts —
        the caller notifies only the sender. Returns
        {"action": "enabled"|"disabled", "priest_ids": [...]}."""
        others = [pid for pid in self._order if pid != triggering_priest_id]
        if not others:
            return {"action": "enabled", "priest_ids": []}
        any_muted = any(self._get_availability(pid).get("notifications_muted") for pid in others)
        mute = not any_muted
        changed: list[str] = []
        for pid in others:
            avail = self._get_availability(pid)
            if bool(avail.get("notifications_muted")) == mute:
                continue
            self._availability[pid] = {**avail, "notifications_muted": mute}
            changed.append(pid)
        if changed:
            self._log(
                "execute_order_66",
                triggered_by=triggered_by,
                reason="disable notifications" if mute else "enable notifications",
                muted=mute,
                priest_ids=changed,
            )
            self._save()
        return {"action": "disabled" if mute else "enabled", "priest_ids": changed}

    # ---------- pending confirmations ----------

    def set_pending_confirmation(self, priest_id: str, entry: dict[str, Any]) -> None:
        stamped = dict(entry)
        if "started_at" not in stamped:
            stamped["started_at"] = california_now().isoformat()
        self._pending_confirmations[priest_id] = stamped
        self._save()

    def touch_pending_confirmation(self, priest_id: str) -> None:
        entry = self._pending_confirmations.get(priest_id)
        if entry is None:
            return
        entry["started_at"] = california_now().isoformat()
        self._save()

    def expire_stale_pendings(
        self,
        timeout_seconds: int = 300,
        skip_types: set[str] | None = None,
        only_types: set[str] | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        """Drop pending menus with no reply for timeout_seconds. Returns
        (priest_id, entry) for each expired prompt."""
        now = california_now()
        expired: list[tuple[str, dict[str, Any]]] = []
        for pid, entry in list(self._pending_confirmations.items()):
            ptype = entry.get("type")
            if skip_types and ptype in skip_types:
                continue
            if only_types and ptype not in only_types:
                continue
            started = entry.get("started_at")
            if not started:
                continue
            try:
                started_dt = as_california_datetime(datetime.fromisoformat(started))
            except ValueError:
                continue
            if (now - started_dt).total_seconds() >= timeout_seconds:
                expired.append((pid, dict(entry)))
                self._pending_confirmations.pop(pid, None)
        if expired:
            self._save()
        return expired

    def pending_confirmation(self, priest_id: str) -> dict[str, Any] | None:
        return self._pending_confirmations.get(priest_id)

    def pop_pending_confirmation(self, priest_id: str) -> dict[str, Any] | None:
        entry = self._pending_confirmations.pop(priest_id, None)
        if entry is not None:
            self._save()
        return entry

    def all_pending_confirmations(self) -> dict[str, dict[str, Any]]:
        return dict(self._pending_confirmations)

    # ---------- read ----------

    def current_order(self, on_date: date | datetime | None = None) -> list[dict[str, Any]]:
        """Ring order, each entry the full priest record plus live
        availability info. Includes unavailable priests
        (unlike effective_order()) so the dashboard can show everyone
        with a status badge rather than hiding them."""
        by_id = self._priests_by_id()
        today = effective_ring_date(california_now() if on_date is None else on_date)
        result = []
        for pid in self._order:
            record = dict(by_id.get(pid, {"id": pid, "name": pid}))
            avail = self._get_availability(pid)
            record["manual_disabled"] = avail.get("manual_disabled", False)
            record["day_off"] = avail.get("day_off")
            # A vacation is a one-time dated range, unlike the recurring
            # day-off/recollection settings - once its end date is behind
            # us, stop showing it on STATUS/the dashboard (both read this
            # method). The stored value is untouched here; SETTINGS/the
            # dashboard's "Clear vacation" button still work on the real
            # data, and a new vacation can be set over a stale one same
            # as always. Purely a display filter.
            vacation = avail.get("vacation")
            if vacation and date.fromisoformat(vacation["end"]) < today:
                vacation = None
            record["vacation"] = vacation
            record["day_of_recollection"] = avail.get("day_of_recollection")
            record["notifications_muted"] = avail.get("notifications_muted", False)
            record["available_today"] = self.is_available(pid)
            result.append(record)
        return result

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(reversed(self._history[-limit:]))

    # ---------- mutate ----------

    def _merge_effective_into_order(self, new_effective_ids: list[str]) -> None:
        """Adopt a new live ring. Priests who are off (day off,
        recollection, vacation, disable) keep their seats; only the
        currently-available names are rewritten, in the new order."""
        available = {pid for pid in self._order if self.is_available(pid)}
        if set(new_effective_ids) != available:
            raise RotationError("new ring does not match who is currently available")
        incoming = iter(new_effective_ids)
        self._order = [next(incoming) if pid in available else pid for pid in self._order]

    def adopt_live_ring(self, live_ids: list[str], triggered_by: str = "system-audit") -> None:
        """Accept a live ring (for example from RingCentral) as correct.

        Priests on that ring are rewritten into that sequence. Anyone
        not listed keeps their seat when the live set matches who is
        available; otherwise the live names come first and the rest
        follow in their previous order. Schedules are not changed.
        """
        known = [pid for pid in live_ids if pid in self._order]
        if not known:
            return
        before = list(self._order)
        available = {pid for pid in self._order if self.is_available(pid)}
        if set(known) == available:
            self._merge_effective_into_order(known)
        else:
            rest = [pid for pid in self._order if pid not in known]
            self._order = known + rest
        self._last_applied_order = list(known)
        self._last_notified_lead_id = known[0]
        if before != self._order:
            self._log(
                "adopt_live_ring",
                triggered_by=triggered_by,
                reason="accepted live ring as saved order",
                order_before=before,
                order_after=list(self._order),
            )
        self._save()

    def rotate(self, triggered_by: str, reason: str = "") -> list[dict[str, Any]]:
        """Move saved #1 to the back. Everyone shifts, including
        priests who are off. They stay off until 8pm; then they take
        whatever seat they landed in (including #1). Day off,
        recollection, and vacation settings are not edited.
        """
        if len(self._order) < 2:
            raise RotationError("Need at least 2 active priests to rotate.")

        before = list(self._order)
        first = self._order.pop(0)
        self._order.append(first)
        # Notify against who is actually ringing now (may not be saved #1).
        effective = self.effective_order()
        self._last_notified_lead_id = effective[0]["id"] if effective else self._order[0]

        self._log(
            "rotate",
            triggered_by=triggered_by,
            reason=reason,
            order_before=before,
            order_after=list(self._order),
        )
        self._save()
        return self.current_order()

    def manual_override(self, new_order: list[str], triggered_by: str, reason: str = "") -> list[dict[str, Any]]:
        """Set an arbitrary order directly (web dashboard 'manual override')."""
        active_ids = {p["id"] for p in load_priests(self.config_path) if p.get("active", True)}
        if set(new_order) != active_ids:
            raise RotationError(
                f"manual_override order {new_order} does not match active priests {sorted(active_ids)}"
            )
        before = list(self._order)
        self._order = list(new_order)
        self._log(
            "manual_override",
            triggered_by=triggered_by,
            reason=reason,
            order_before=before,
            order_after=list(self._order),
        )
        self._save()
        return self.current_order()

    def add_priest(self, priest: dict[str, Any], triggered_by: str) -> None:
        priests = load_priests(self.config_path)
        if any(p["id"] == priest["id"] for p in priests):
            raise RotationError(f"priest id '{priest['id']}' already exists")
        priests.append(priest)
        save_priests(self.config_path, priests)
        if priest.get("active", True):
            self._order.append(priest["id"])
            self._availability[priest["id"]] = self._default_availability()
        self._log("add_priest", triggered_by=triggered_by, priest_id=priest["id"])
        self._save()

    def remove_priest(self, priest_id: str, triggered_by: str) -> None:
        priests = load_priests(self.config_path)
        remaining = [p for p in priests if p["id"] != priest_id]
        if len(remaining) == len(priests):
            raise RotationError(f"priest id '{priest_id}' not found")
        save_priests(self.config_path, remaining)
        if priest_id in self._order:
            self._order.remove(priest_id)
        self._availability.pop(priest_id, None)
        self._pending_confirmations.pop(priest_id, None)
        self._log("remove_priest", triggered_by=triggered_by, priest_id=priest_id)
        self._save()

    def swap(self, priest_id_a: str, priest_id_b: str, triggered_by: str) -> list[dict[str, Any]]:
        if priest_id_a not in self._order or priest_id_b not in self._order:
            raise RotationError("both priests must be in the active order to swap")
        i, j = self._order.index(priest_id_a), self._order.index(priest_id_b)
        self._order[i], self._order[j] = self._order[j], self._order[i]
        self._log(
            "swap",
            triggered_by=triggered_by,
            reason=f"{priest_id_a} <-> {priest_id_b}",
            order_after=list(self._order),
        )
        self._save()
        return self.current_order()
