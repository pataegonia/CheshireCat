"""통합 테스트 픽스처: 루트 app.py가 만든 실제 app을 가짜 LLM 전송으로 띄운다(I1.4)."""

import contextlib
import importlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

_CLEARED_ENV = (
    "GUARD_CONFIG",
    "GUARD_KB_DIR",
    "GUARD_PUBLIC_DIR",
    "GUARD_PROMPTS_DIR",
    "TUTOR_CONFIG",
    "OPENAI_API_KEY",  # 없으면 실제 전송 함수는 보내기 전에 실패한다(실제 API를 부를 수 없다)
    "OPENAI_BASE_URL",
)
LLM_MODES = ("v2", "baseline")
_MODE_LINE = re.compile(r"^mode:[ \t]*v2(?P<rest>[ \t]*(?:#.*)?)$", re.M)


@pytest.fixture
def app_env(monkeypatch):
    """가짜 토큰 · 모델 이름만 넣고, 실제 키와 설정 경로 환경변수는 지운다."""
    from tests.integration.applib import TOKEN

    for name in _CLEARED_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TEAM_TOKEN", TOKEN)
    monkeypatch.setenv("OPENAI_MODEL", "fake-model")


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

    v2는 저장소 설정 그대로(`config/tutor.yaml` 기본값, 검색 → 주 호출 → 결정적 수정)이고,
    baseline은 mode만 바꾼 복사본을 쓴다(검색 → 일반 프롬프트 1회).
    """
    text = (ROOT / "config" / "tutor.yaml").read_text(encoding="utf-8")
    # 줄 시작의 최상위 키만 바꾼다(주석 안의 같은 글자는 건드리지 않는다). 정확히 한 줄이어야 한다.
    changed, count = _MODE_LINE.subn(r"mode: baseline\g<rest>", text)
    assert count == 1  # 기본값이 v2가 아니게 되거나 키가 둘이면 여기서 걸린다
    if request.param == "baseline":
        path = tmp_path / "tutor.yaml"
        path.write_text(changed, encoding="utf-8")
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
def client(open_app):
    """저장소 설정 그대로(config/tutor.yaml) 띄운 앱."""
    with open_app() as c:
        yield c


@pytest.fixture
def llm_client(llm_mode, open_app):
    """튜터가 가짜 LLM을 실제로 부르는 앱. v2와 baseline으로 한 번씩 돈다."""
    with open_app() as c:
        assert c.app.state.tutor_cfg.mode == llm_mode
        yield c
