import os

import pytest
from fastapi.testclient import TestClient

from tests.tutor.tutor_testlib import TOKEN, make_app
from tutor import pipeline

GUARD_ENV = ("GUARD_CONFIG", "GUARD_KB_DIR", "GUARD_PUBLIC_DIR", "GUARD_PROMPTS_DIR")
# 테스트 모듈을 불러올 때(수집 단계) 색인을 만들어 가드 설정을 읽는 곳이 있다(@cache). 아래 _env는
# 그 뒤에 돌므로, 셸의 가드 설정이 그 값을 물지 않게 수집 전에 지운다
for _name in GUARD_ENV:
    os.environ.pop(_name, None)

API_KEYS = ("OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL")
API_KEYS += ("EMBEDDING_API_KEY", "EMBEDDING_MODEL", "EMBEDDING_BASE_URL")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in GUARD_ENV:
        monkeypatch.delenv(name, raising=False)
    for name in API_KEYS:  # 런박스 셸에 진짜 키가 있어도 테스트가 운영진 API를 부르지 않게
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TEAM_TOKEN", TOKEN)
    # handle의 기본 state(프로세스에 하나, @cache)를 테스트마다 새로 만든다. 그래야 가짜 전송이
    # 늘 적용되고, 앞 테스트의 호출·실패 수가 넘어오지 않는다
    pipeline.default_state.cache_clear()
    yield
    pipeline.default_state.cache_clear()


@pytest.fixture
def client():
    with TestClient(make_app()) as c:
        yield c
