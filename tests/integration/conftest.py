"""통합 테스트 픽스처: 루트 app.py가 만든 실제 app을 가짜 LLM 전송으로 띄운다(I1.4)."""

import contextlib
import importlib
import logging
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

_CLEARED_ENV = (
    "GUARD_CONFIG",
    "GUARD_OVERLAY",  # 실험 덮개(exp/guard-jamo-longinput)
    "GUARD_KB_DIR",
    "GUARD_PUBLIC_DIR",
    "GUARD_PROMPTS_DIR",
    "TUTOR_CONFIG",
    "OPENAI_API_KEY",  # 없으면 실제 전송 함수는 보내기 전에 실패한다(실제 API를 부를 수 없다)
    "OPENAI_BASE_URL",
    # 저장소 설정 그대로의 앱(영어 장치 켬)이 기동 때 실제 임베딩 API를 부르지 않게 한다
    "EMBEDDING_API_KEY",
    "EMBEDDING_MODEL",
    "EMBEDDING_BASE_URL",
)
LLM_MODES = ("v2", "baseline")
# 가드 LLM이 기동 때 읽는 API 설정의 가짜 값. 패치된 `guardrail.llm.openai_env`만 이 값을 돌려주고,
# 환경변수에는 넣지 않는다. 주소가 이 컴퓨터의 닫힌 포트인 것은 "가드 LLM이 가짜(OpenAIShim) 대신
# 진짜 클라이언트를 만든 경우"만 막는다. 그 밖의 코드(튜터 · 다른 모듈의 openai 사용)가 실제 API로
# 나가지 못하는 것은 `app_env`가 OPENAI_API_KEY · OPENAI_BASE_URL 환경변수를 지우기 때문이다.
GUARD_LLM_ENV = {
    "OPENAI_API_KEY": "fake-guard-key",
    "OPENAI_MODEL": "fake-model",
    "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
}


@pytest.fixture
def fake_guard_send(monkeypatch):
    """가드 LLM(`guardrail.llm.LLM`)의 전송만 가짜로 바꾼다. 게이트 · 재시도 · 카운터는 실제다.

    가드 LLM은 `start()`에서 `openai_env()`로 설정을 읽고 `openai.AsyncOpenAI`로 클라이언트를
    만든다. 그 두 이름을 `guardrail.llm` 모듈 안에서만 바꾼다(환경변수와 튜터가 쓰는 openai 모듈은
    그대로 둔다). 루트 앱의 기본 판정기(가드 `default_factory`)는 OPENAI_API_KEY가 없으면 상수
    판정기라, 키를 지우는 `app_env`로 띄운 앱에서는 이 가짜가 불리지 않는다. 진짜 판정기로 여는
    `real_guard_client`(test_real_guard.py)와 판정기 대역(`open_llm_guard_app`)의 nano 호출이
    여기로 온다.

    패치와 끝날 때의 `unknown` 단언은 `applib.patched_guard_llm`에 있다(테스트가 그 단언을 직접
    돌려 볼 수 있게 일반 제너레이터로 뺐다).
    """
    from tests.integration.applib import patched_guard_llm

    yield from patched_guard_llm(monkeypatch, GUARD_LLM_ENV)


@pytest.fixture(scope="session")
def baseline_tutor_config(tmp_path_factory):
    """기준 설정 사본: 저장소 config/tutor.yaml에서 동결 토글(`applib.FREEZE_TOGGLES_OFF`)만 끈 것.

    세션에 한 번 만든다. 저장소 설정에 아직 없는 키는 건드리지 않는다.
    """
    from tests.integration.applib import write_tutor_config

    return write_tutor_config(tmp_path_factory.mktemp("tutor_config") / "tutor_baseline.yaml")


@pytest.fixture
def app_env(monkeypatch, fake_guard_send, baseline_tutor_config):
    """가짜 토큰 · 모델 이름만 넣고, 실제 키와 설정 경로 환경변수는 지운다. 튜터 설정은 기준 설정
    사본(`baseline_tutor_config`, 동결 토글 끔)이다.

    앱을 띄우는 테스트는 모두 이 픽스처를 지난다. 가드 LLM의 가짜 전송(`fake_guard_send`)을 여기에
    묶어, 어떤 방법으로 앱을 띄우든 가드 nano 호출이 실제 API로 나가지 않게 한다.
    """
    from tests.integration.applib import TOKEN

    for name in _CLEARED_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TEAM_TOKEN", TOKEN)
    monkeypatch.setenv("OPENAI_MODEL", "fake-model")
    monkeypatch.setenv("TUTOR_CONFIG", str(baseline_tutor_config))


@pytest.fixture
def tutor_guard_stages_on(app_env, tmp_path, monkeypatch):
    """튜터의 가드 단계를 켠 설정 사본(`applib.TUTOR_GUARD_STAGES_ON`)을 TUTOR_CONFIG로 넣는다.

    튜터→가드 연결(부르는 횟수 · 건너뜀 · nano 상한 · 요청 내용)을 보는 앱은 기본값이 아니라 이
    픽스처로 두 단계를 켜고 띄운다. 기본값(끔)은 test_real_guard.py가 따로 본다.
    """
    from tests.integration.applib import TUTOR_GUARD_STAGES_ON, write_tutor_config

    path = write_tutor_config(tmp_path / "tutor-stages-on.yaml", **TUTOR_GUARD_STAGES_ON)
    monkeypatch.setenv("TUTOR_CONFIG", str(path))
    return path


@pytest.fixture
def fake_send(monkeypatch):
    """튜터 LLM의 전송 함수만 가짜로 바꾼다. 게이트 · 카운터 · 상한은 실제 TutorLLM 그대로다."""
    from tests.integration.applib import FakeSend

    fake = FakeSend()
    llm_mod = importlib.import_module("tutor.runtime.llm")
    monkeypatch.setattr(llm_mod, "_openai_send", lambda: fake)
    yield fake
    # 튜터는 전송 예외를 오류 코드로 바꿔 삼킨다. 가짜가 모르는 호출이 있었으면 여기서 드러낸다.
    assert fake.unknown == [], "FakeSend가 모르는 스키마로 LLM을 불렀다(applib.FakeSend를 고친다)"


@pytest.fixture(params=LLM_MODES)
def llm_mode(request, tmp_path, monkeypatch, app_env):
    """LLM을 부르는 두 경로를 차례로 돈다. 값은 모드 이름이다.

    v2는 기준 설정(동결 토글 끔, `app_env`의 사본. 검색 → 주 호출 → 결정적 수정)이고, baseline은
    그 위에 mode만 바꾼 사본을 쓴다(검색 → 일반 프롬프트 1회). 저장소 기본 mode가 v2가 아니게 되면
    `llm_client`의 mode 단언이 걸린다.
    """
    from tests.integration.applib import write_tutor_config

    if request.param == "baseline":
        path = write_tutor_config(tmp_path / "tutor.yaml", mode="baseline")
        monkeypatch.setenv("TUTOR_CONFIG", str(path))
    return request.param


@pytest.fixture
def open_app(app_env, fake_send):
    """루트 app.py를 새로 import해 만든 실제 app을 TestClient로 여는 함수(부를 때마다 새 앱)."""
    from fastapi.testclient import TestClient

    @contextlib.contextmanager
    def _open():
        module = importlib.reload(importlib.import_module("app"))
        with TestClient(module.app) as client:
            yield client

    return _open


@pytest.fixture
def open_llm_guard_app(app_env, fake_send, tutor_guard_stages_on):
    """루트 app.py와 같은 조립에서 판정기만 LLM을 쓰는 대역(`llm_guard`)으로 바꾼 앱을 여는 함수.

    대역에는 튜터가 맡기는 classify · judge_output이 있다. 지금 main의 Guard에는 둘이 없어(#119)
    튜터→가드 호출은 이 앱으로만 본다. 가드가 올리고 test_real_guard.py가 그 경로를 덮으면 지운다.
    튜터 설정은 두 단계를 켠 사본이다(`tutor_guard_stages_on`).
    """
    from fastapi.testclient import TestClient

    @contextlib.contextmanager
    def _open():
        from guardrail.server import create_app

        import tutor
        from tests.integration.llm_guard import llm_backed_factory

        app = create_app(
            llm_backed_factory,
            tutor_router=tutor.router,
            defaults={"/tutor": tutor.TUTOR_DEFAULT},
            startups=[tutor.startup],
        )
        with TestClient(app) as client:
            yield client

    return _open


# 진짜 판정기를 고르게 하는 API 키의 가짜 값. 환경변수에 넣어 가드 `default_factory`가
# `Guard.build`를 고르게 한다. 가드 LLM의 전송은 `fake_guard_send`가, 튜터 전송은 `fake_send`가
# 가짜로 바꿔 두므로 이 값으로 실제 API를 부르는 코드는 없다.
REAL_GUARD_KEY = "fake-real-guard-key"


@pytest.fixture
def real_guard_env(monkeypatch, app_env):
    """`app_env`에 가짜 OPENAI_API_KEY와 닫힌 주소 OPENAI_BASE_URL을 넣는다. 루트 앱이 진짜
    판정기(`Guard.build`)로 뜬다.

    기존 테스트(`app_env` · `client`)는 키가 없어 상수 판정기로 돈다. 진짜 판정기로 여는 앱은
    `test_real_guard.py`에서만 쓴다(전부 진짜 판정기로 돌리면 몇 분이 걸린다).
    """
    monkeypatch.setenv("OPENAI_API_KEY", REAL_GUARD_KEY)
    # 가짜 전송을 거치지 않는 코드가 생겨도 실제 주소가 아니라 닫힌 주소로 가서 바로 실패하게 한다
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")


@pytest.fixture
def open_real_guard_app(real_guard_env, fake_send):
    """`real_guard_env`로 루트 app.py를 여는 함수(부를 때마다 새 앱). 진짜 판정기가 아니면 열자마자
    실패한다. 튜터 설정을 바꾼 앱이 필요하면 부르기 전에 TUTOR_CONFIG를 넣는다."""
    from fastapi.testclient import TestClient

    @contextlib.contextmanager
    def _open():
        module = importlib.reload(importlib.import_module("app"))
        with TestClient(module.app) as c:
            # 기동이 실패하면 가드는 앱을 살리려고 상수 판정기로 조용히 내린다. 여기서 드러낸다
            assert type(c.app.state.guard).__name__ == "Guard", type(c.app.state.guard).__name__
            yield c

    return _open


@pytest.fixture
def real_guard_client(open_real_guard_app, tutor_guard_stages_on, caplog):
    """튜터의 가드 단계를 켠 설정(`tutor_guard_stages_on`)으로 진짜 판정기와 함께 연 TestClient.

    기동 로그를 보려고 caplog를 DEBUG로 둔다(기동 기록은 `caplog.get_records("setup")`).
    """
    caplog.set_level(logging.DEBUG)
    with open_real_guard_app() as c:
        toggles = c.app.state.tutor_cfg.toggles
        assert (toggles.guard_classify, toggles.verify) == ("parallel", "guard")
        yield c


@pytest.fixture
def client(open_app):
    """기준 설정(동결 토글 끔, `baseline_tutor_config`)으로 띄운 앱."""
    with open_app() as c:
        yield c


@pytest.fixture
def repo_config_client(app_env, fake_send, monkeypatch):
    """저장소 설정 그대로(config/tutor.yaml, 동결 토글 포함) 띄운 앱. TUTOR_CONFIG를 지우고 루트
    app.py를 다시 import해 연다. test_freeze_config.py만 쓴다."""
    from fastapi.testclient import TestClient

    monkeypatch.delenv("TUTOR_CONFIG", raising=False)
    module = importlib.reload(importlib.import_module("app"))
    with TestClient(module.app) as c:
        yield c


@pytest.fixture
def llm_client(llm_mode, open_app):
    """튜터가 가짜 LLM을 실제로 부르는 앱. v2와 baseline으로 한 번씩 돈다."""
    with open_app() as c:
        assert c.app.state.tutor_cfg.mode == llm_mode
        yield c
