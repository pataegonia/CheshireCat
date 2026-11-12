"""통합 테스트 픽스처: 루트 app.py가 만든 실제 app을 가짜 LLM 전송으로 띄운다(I1.4)."""

import contextlib
import importlib
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
    return fake


@pytest.fixture
def baseline_mode(tmp_path, monkeypatch, app_env):
    """튜터 설정을 mode: baseline으로 바꾼 복사본을 쓴다(지금 LLM을 부르는 경로는 이것뿐이다)."""
    text = (ROOT / "config" / "tutor.yaml").read_text(encoding="utf-8")
    assert "mode: v2" in text
    path = tmp_path / "tutor.yaml"
    path.write_text(text.replace("mode: v2", "mode: baseline", 1), encoding="utf-8")
    monkeypatch.setenv("TUTOR_CONFIG", str(path))


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
def baseline_client(baseline_mode, open_app):
    """튜터가 가짜 LLM을 실제로 부르는 앱(mode: baseline)."""
    with open_app() as c:
        yield c
