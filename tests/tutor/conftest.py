import pytest
from fastapi.testclient import TestClient

from tests.tutor.tutor_testlib import TOKEN, make_app


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("GUARD_CONFIG", "GUARD_KB_DIR", "GUARD_PUBLIC_DIR", "GUARD_PROMPTS_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TEAM_TOKEN", TOKEN)


@pytest.fixture
def client():
    with TestClient(make_app()) as c:
        yield c
