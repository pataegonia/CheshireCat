import pytest
from fastapi.testclient import TestClient

from tests.tutor.tutor_testlib import TOKEN, make_app

API_KEYS = ("OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL")
API_KEYS += ("EMBEDDING_API_KEY", "EMBEDDING_MODEL", "EMBEDDING_BASE_URL")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("GUARD_CONFIG", "GUARD_KB_DIR", "GUARD_PUBLIC_DIR", "GUARD_PROMPTS_DIR"):
        monkeypatch.delenv(name, raising=False)
    for name in API_KEYS:  # 런박스 셸에 진짜 키가 있어도 테스트가 운영진 API를 부르지 않게
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TEAM_TOKEN", TOKEN)


@pytest.fixture
def client():
    with TestClient(make_app()) as c:
        yield c
