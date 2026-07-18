from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def fixture_json():
    directory = Path(__file__).parent / "fixtures"

    def load(name: str):
        return json.loads((directory / name).read_text(encoding="utf-8"))

    return load
