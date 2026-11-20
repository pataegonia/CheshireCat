import pytest
import yaml
from fastapi.testclient import TestClient

from tests.tutor.tutor_testlib import TOKEN, make_app
from tutor.runtime import config as tutor_config

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


# 동결 설정(11/18)이 켠 토글. 튜터 단위 테스트는 이 토글들을 끈 기준 설정에서 돌고, 켠 동작은 각
# 테스트가 overrides로 켜서 본다(저장소 기본값을 바꿀 때마다 테스트 기준이 흔들리지 않게).
# 저장소 설정 파일 그대로의 값은 test_tutor_config가 파일을 직접 읽어 본다. 겹쳐 보내기
# (`llm.timeout_s.main_hedge`)·직렬 T 단축 대기(`intent_wait`)도 아래 사본에서 끈다
FREEZE_TOGGLES = {
    "delegation_override": False,
    "fallback_quote": False,
    "intent_override": "off",
    "quote_append": False,
    "quote_from_top1": False,
    "oom_gate": "off",
    "oom_schedule": "with_request",
    "english_embedding": False,  # E(oom_gate)를 끈 기준에서는 켤 수 없다(로드에서 거부)
    "code_block_lang": False,  # 켠 동작은 test_code_block_lang이 overrides로 본다
    "top1_redraft": False,  # 켠 동작은 test_top1_redraft가 overrides로 본다
}


@pytest.fixture(scope="session")
def _baseline_config(tmp_path_factory):
    """저장소 config/tutor.yaml에서 동결 토글만 끈 사본(세션에 한 번)."""
    repo = tutor_config.ROOT / tutor_config.DEFAULT_CONFIG
    raw = yaml.safe_load(repo.read_text(encoding="utf-8"))
    raw["toggles"].update(FREEZE_TOGGLES)
    # 겹쳐 보내기·직렬 T 단축 대기도 끈 값으로 둔다(켠 동작은 test_main_hedge·test_intent가 본다)
    timeouts = raw["llm"]["timeout_s"]
    timeouts.update(main_hedge=timeouts["main"], intent_wait=timeouts["intent"])
    # 실험 multi_draft(주 호출 초안 여러 개)도 끈다(켠 동작은 test_multi_draft가 본다)
    if "multi_draft" in raw["llm"]:
        raw["llm"]["multi_draft"]["enabled"] = False
    path = tmp_path_factory.mktemp("tutor_config") / "tutor_baseline.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _env(monkeypatch, _baseline_config):
    monkeypatch.setenv(tutor_config.CONFIG_ENV, str(_baseline_config))
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
