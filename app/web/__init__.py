"""Password-protected web dashboard.

One shared admin login (WEB_ADMIN_USERNAME / WEB_ADMIN_PASSWORD in .env)
via HTTP Basic Auth - deliberately simple. This is a rectory-based
internal tool behind whatever network/VPN the parish already uses, not
a public-facing app, so we're not building out per-user accounts,
password reset flows, etc. If that ever changes, swap this auth layer
out; nothing else in the app depends on how auth works.
"""
from __future__ import annotations

import functools
import os
from datetime import date
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template, request, url_for

from app.notifier import EmailConfig, Notifier
from app.ringcentral_client import RingCentralDriverError, build_driver
from app.failsafe import enter_manual_failsafe
from app.localtime import format_california
from app.rotation import WEEKDAY_ORDER, RotationError, RotationManager
from app.signal_client import SignalClient


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    cfg = {**os.environ, **(config or {})}
    app.secret_key = cfg.get("FLASK_SECRET_KEY", "dev-key-change-me")

    base_dir = Path(__file__).resolve().parent.parent.parent
    rotation = RotationManager(
        config_path=base_dir / "config" / "priests.yaml",
        state_path=base_dir / "data" / "state.json",
    )
    rc_driver = build_driver(cfg)
    signal_client = SignalClient(
        signal_cli_path=cfg.get("SIGNAL_CLI_PATH", "signal-cli"),
        bot_number=cfg.get("SIGNAL_BOT_NUMBER", ""),
    )
    email_config = EmailConfig(
        smtp_host=cfg.get("SMTP_HOST", ""),
        smtp_port=int(cfg.get("SMTP_PORT", 587)),
        smtp_username=cfg.get("SMTP_USERNAME", ""),
        smtp_password=cfg.get("SMTP_PASSWORD", ""),
        smtp_from=cfg.get("SMTP_FROM", ""),
        alert_to=[e.strip() for e in cfg.get("ALERT_EMAIL_TO", "").split(",") if e.strip()],
    )
    def _alert_cells() -> list[str]:
        return [p["cell_number"] for p in rotation.current_order() if p.get("cell_number")]

    notifier = Notifier(
        signal_client,
        email_config,
        alert_numbers=_alert_cells(),
        sms_driver=rc_driver,
        numbers_provider=_alert_cells,
    )

    app.config["ROTATION"] = rotation
    app.config["RC_DRIVER"] = rc_driver
    app.config["NOTIFIER"] = notifier
    app.config["ADMIN_USERNAME"] = cfg.get("WEB_ADMIN_USERNAME", "admin")
    app.config["ADMIN_PASSWORD"] = cfg.get("WEB_ADMIN_PASSWORD", "changeme")

    def require_auth(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            auth = request.authorization
            if not auth or not (
                auth.username == app.config["ADMIN_USERNAME"]
                and auth.password == app.config["ADMIN_PASSWORD"]
            ):
                return Response(
                    "Login required.", 401, {"WWW-Authenticate": 'Basic realm="Sacramental Line Admin"'}
                )
            return view(*args, **kwargs)

        return wrapped

    @app.before_request
    def _reload_rotation_state():
        # This process's Flask app has its own RotationManager instance,
        # separate from the Signal bot's (see app/main.py) - reload from
        # state.json before every request so neither reads stale data
        # nor overwrites a change the other just made.
        rotation.reload()

    @app.route("/")
    @require_auth
    def dashboard():
        by_id = {p["id"]: p["name"] for p in rotation.current_order()}
        pending = [
            {"priest_id": pid, "priest_name": by_id.get(pid, pid), **entry}
            for pid, entry in rotation.all_pending_confirmations().items()
        ]
        history = []
        for entry in rotation.history(limit=25):
            row = dict(entry)
            if row.get("timestamp"):
                row["timestamp"] = format_california(row["timestamp"])
            history.append(row)
        return render_template(
            "dashboard.html",
            order=rotation.current_order(),
            history=history,
            manual_mode=rc_driver.requires_manual_step,
            pending_confirmations=pending,
            weekday_order=WEEKDAY_ORDER,
            automation_enabled=rotation.automation_enabled,
        )

    @app.route("/rotate", methods=["POST"])
    @require_auth
    def do_rotate():
        try:
            new_order = rotation.rotate(triggered_by="web-dashboard", reason="manual rotate via dashboard")
            # Push the availability-filtered order, not the raw one - see
            # the same note in app/signal_bot.py's _handle_rotate.
            rc_driver.apply_order(rotation.effective_order())
            rotation.mark_applied_order([p["id"] for p in rotation.effective_order()])
            numbers = rotation.notifiable_numbers(new_order)
            names = " -> ".join(p["name"] for p in new_order)
            notifier.notify_all(numbers, f"Rotation triggered from the admin dashboard.\nNew order: {names}")
        except RotationError as exc:
            notifier.alert(f"Dashboard rotation failed: {exc}")
        except RingCentralDriverError as exc:
            enter_manual_failsafe(
                rotation,
                signal_client,
                rc_driver,
                f"Dashboard rotation failed to update RingCentral: {exc}",
                notifier=notifier,
            )
        return redirect(url_for("dashboard"))

    @app.route("/override", methods=["POST"])
    @require_auth
    def override():
        new_order = request.form.getlist("order")
        try:
            rotation.manual_override(new_order, triggered_by="web-dashboard", reason="manual override")
        except RotationError as exc:
            notifier.alert(f"Manual override failed: {exc}")
            return redirect(url_for("dashboard"))
        # Next Signal poll (3s) will push RC and text the new lineup,
        # including if #2 or #3 was moved to #1.
        return redirect(url_for("dashboard"))

    @app.route("/priests/add", methods=["POST"])
    @require_auth
    def add_priest():
        priest = {
            "id": request.form["id"].strip(),
            "name": request.form["name"].strip(),
            "extension": request.form.get("extension", "").strip(),
            "cell_number": request.form.get("cell_number", "").strip(),
            "ring_count": int(request.form.get("ring_count", 4)),
            "active": True,
        }
        try:
            rotation.add_priest(priest, triggered_by="web-dashboard")
        except RotationError as exc:
            notifier.alert(f"Add priest failed: {exc}")
        return redirect(url_for("dashboard"))

    @app.route("/priests/remove", methods=["POST"])
    @require_auth
    def remove_priest():
        priest_id = request.form["id"].strip()
        try:
            rotation.remove_priest(priest_id, triggered_by="web-dashboard")
        except RotationError as exc:
            notifier.alert(f"Remove priest failed: {exc}")
        return redirect(url_for("dashboard"))

    @app.route("/priests/swap", methods=["POST"])
    @require_auth
    def swap_priests():
        a, b = request.form["id_a"].strip(), request.form["id_b"].strip()
        try:
            rotation.swap(a, b, triggered_by="web-dashboard")
        except RotationError as exc:
            notifier.alert(f"Swap failed: {exc}")
        return redirect(url_for("dashboard"))

    @app.route("/priests/<priest_id>/disable", methods=["POST"])
    @require_auth
    def set_disable(priest_id):
        disabled = request.form.get("disabled") == "true"
        try:
            rotation.set_manual_disable(priest_id, disabled, triggered_by="web-dashboard")
        except RotationError as exc:
            notifier.alert(f"Set manual disable failed: {exc}")
        return redirect(url_for("dashboard"))

    @app.route("/priests/<priest_id>/mute", methods=["POST"])
    @require_auth
    def set_mute(priest_id):
        muted = request.form.get("muted") == "true"
        try:
            rotation.set_notifications_muted(priest_id, muted, triggered_by="web-dashboard")
        except RotationError as exc:
            notifier.alert(f"Set notification mute failed: {exc}")
        return redirect(url_for("dashboard"))

    @app.route("/priests/<priest_id>/day-off", methods=["POST"])
    @require_auth
    def set_day_off(priest_id):
        weekday = request.form.get("day_off", "").strip() or None
        try:
            rotation.set_day_off(priest_id, weekday, triggered_by="web-dashboard")
        except RotationError as exc:
            notifier.alert(f"Set day off failed: {exc}")
        return redirect(url_for("dashboard"))

    @app.route("/priests/<priest_id>/vacation", methods=["POST"])
    @require_auth
    def set_vacation(priest_id):
        start_str = request.form.get("start", "").strip()
        end_str = request.form.get("end", "").strip()
        try:
            start = date.fromisoformat(start_str) if start_str else None
            end = date.fromisoformat(end_str) if end_str else None
            rotation.set_vacation(priest_id, start, end, triggered_by="web-dashboard")
        except (RotationError, ValueError) as exc:
            notifier.alert(f"Set vacation failed: {exc}")
        return redirect(url_for("dashboard"))

    @app.route("/priests/<priest_id>/recollection", methods=["POST"])
    @require_auth
    def set_recollection(priest_id):
        ordinal_str = request.form.get("ordinal", "").strip()
        try:
            ordinal = int(ordinal_str) if ordinal_str else None
            rotation.set_day_of_recollection(priest_id, ordinal, triggered_by="web-dashboard")
        except (RotationError, ValueError) as exc:
            notifier.alert(f"Set day of recollection failed: {exc}")
        return redirect(url_for("dashboard"))

    @app.route("/automation/toggle", methods=["POST"])
    @require_auth
    def toggle_automation():
        enabled = request.form.get("enabled") == "true"
        try:
            rotation.set_global_automation(enabled, triggered_by="web-dashboard")
            rc_driver.apply_order(rotation.effective_order())
            rotation.mark_applied_order([p["id"] for p in rotation.effective_order()])
        except RotationError as exc:
            notifier.alert(f"Toggle automation failed: {exc}")
        except RingCentralDriverError as exc:
            notifier.alert(f"Toggle automation saved, but RingCentral update failed: {exc}")
        return redirect(url_for("dashboard"))

    @app.route("/api/status")
    @require_auth
    def api_status():
        return jsonify(
            {
                "order": rotation.current_order(),
                "history": rotation.history(limit=25),
                "manual_mode": rc_driver.requires_manual_step,
                "automation_enabled": rotation.automation_enabled,
            }
        )

    return app
