"""The live latch is never inherited from the shell that runs pytest."""
import pytest

from spx_noon.config import LIVE_ACCOUNT_ENV, LIVE_ALLOW_ENV


@pytest.fixture(autouse=True)
def _no_live_latch(monkeypatch):
    monkeypatch.delenv(LIVE_ALLOW_ENV, raising=False)
    monkeypatch.delenv(LIVE_ACCOUNT_ENV, raising=False)
