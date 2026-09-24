from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.fixture
def priests_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "priests.yaml"
    data = {
        "priests": [
            {"id": "fr_martin", "name": "Fr James Martin SJ", "cell_number": "+19165550001", "ring_count": 4, "active": True, "audit_notices": True},
            {"id": "fr_bugnini", "name": "Fr Bugnini SSPX", "cell_number": "+19165550002", "ring_count": 4, "active": True},
            {"id": "fr_youngtrad", "name": "Fr Youngtrad FSSP", "cell_number": "+19165550003", "ring_count": 4, "active": True},
        ]
    }
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f)
    return config_path


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "state.json"
