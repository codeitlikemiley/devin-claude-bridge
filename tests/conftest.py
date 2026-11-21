import os

import pytest


@pytest.fixture(autouse=True)
def no_devin_environment(monkeypatch):
    """Keep a developer's own exported accounts out of every test."""
    for name in list(os.environ):
        if name.startswith(("DEVIN_BRIDGE_", "DEVIN_API_KEY", "DEVIN_ORG_ID")):
            monkeypatch.delenv(name)
