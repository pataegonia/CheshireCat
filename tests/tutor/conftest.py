import pytest
from fastapi.testclient import TestClient

from tests.tutor.tutor_testlib import TOKEN, make_app

GUARD_ENV = ("GUARD_CONFIG", "GUARD_KB_DIR", "GUARD_PUBLIC_DIR", "GUARD_PROMPTS_DIR")

# tutor.judge.guard_merge는 import 때 가드 설정을 읽는다(ATTACK_CATEGORIES). 셸의 가드 설정이
# 그 값을 물지 않게, 테스트 모듈보다 먼저 가드 환경변수를 잠깐 지운 채 불러오고 바로 되돌린다
with pytest.MonkeyPatch.context() as _mp:
    for _name in GUARD_ENV:
        _mp.delenv(_name, raising=False)
    from tutor import pipeline  # tutor.judge.guard_merge까지 불러온다

API_KEYS = ("OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL")
API_KEYS += ("EMBEDDING_API_KEY", "EMBEDDING_MODEL", "EMBEDDING_BASE_URL")


@pytest.fixture(scope="package", autouse=True)
def _no_guard_env():
    """튜터 테스트 동안 셸의 가드 설정(GUARD_*)을 지우고, 패키지가 끝나면 되돌린다.

    패키지 범위라 모듈 범위 픽스처(예: test_embedding의 kb_search)보다 먼저 돈다. 그래서 그 안에서
    처음 만드는 색인(default_index, @cache)도 저장소의 가드 기본 설정으로 만든다.
    """
    with pytest.MonkeyPatch.context() as mp:
        for name in GUARD_ENV:
            mp.delenv(name, raising=False)
        yield


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
