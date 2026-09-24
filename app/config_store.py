"""Load/save the priest roster from config/priests.yaml.

Kept deliberately separate from rotation.py's in-progress *state* (whose
order and call counts change constantly) so the human-editable roster
file only changes when someone is actually adding/removing/swapping a
priest, not on every rotation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_priests(config_path: Path) -> list[dict[str, Any]]:
    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("priests", [])


def save_priests(config_path: Path, priests: list[dict[str, Any]]) -> None:
    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    data["priests"] = priests
    tmp_path = config_path.with_suffix(".yaml.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    tmp_path.replace(config_path)  # atomic on POSIX
