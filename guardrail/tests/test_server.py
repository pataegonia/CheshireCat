"""S1.2 서버: 인증·기동·200 보장·반사 0·로그 규칙 (C1 §S1.2, GR-01·09·12, ADR-0009)."""

import contextlib
import json
import logging
from pathlib import Path

import hypothesis.strategies as st
import jsonschema
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings

import guardrail.server as server
from guardrail.config import ROOT, GuardConfig
from guardrail.guard import ConstantGuard
from guardrail.server import create_app, guard_lifespan, router

TOKEN = "tok-123_abc"  # noqa: S105 — 테스트용 가짜 토큰
AUTH = {"Authorization": f"Bearer {TOKEN}"}
MARK = "MARKER-7f3a9c"
CONTRACT = json.loads((ROOT / "docs" / "contract" / "guard.schema.json").read_text("utf-8"))
RESPONSE_SCHEMA = {**CONTRACT, "$ref": "#/$defs/response"}
PUBLIC = ROOT.parent / "competition" / "public"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("GUARD_CONFIG", "GUARD_KB_DIR", "GUARD_PUBLIC_DIR", "GUARD_PROMPTS_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TEAM_TOKEN", TOKEN)


async def _constant(cfg: GuardConfig) -> ConstantGuard:
    return ConstantGuard()


@pytest.fixture
def client():
    with TestClient(create_app(_constant)) as c:
        yield c


def _post(client, body, headers=AUTH, path="/guard"):
    data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
    return client.post(path, content=data, headers=headers)


def _public_rows():
    rows = []
    for name in ("guard.jsonl", "tutor.jsonl"):
        lines = (PUBLIC / name).read_text("utf-8").splitlines()
        rows += [json.loads(line) for line in lines if line.strip()]
    return rows


# --- 계약 ---


def test_public_rows_all_200_and_match_contract(client):
    rows = _public_rows()
    assert len(rows) == 400
    for row in rows:
        body = {"messages": row["messages"]}
        if "candidate_answer" in row:
            body["candidate_answer"] = row["candidate_answer"]
        r = _post(client, body)
        assert r.status_code == 200
        jsonschema.validate(r.json(), RESPONSE_SCHEMA)
        assert ("output" in r.json()) == ("candidate_answer" in row)


@pytest.mark.parametrize(
    "body",
    [b"", b"not json", b"[1,2]", b'{"messages": 3}', b"\xed\xa0\x80", b'{"candidate_answer": 5}'],
)
def test_bad_bodies_get_default_200(client, body):
    r = _post(client, body)
    assert r.status_code == 200
    jsonschema.validate(r.json(), RESPONSE_SCHEMA)
    assert r.json()["input"] == {"decision": "allow", "category": "normal"}
    assert ("output" in r.json()) == (b"candidate_answer" in body)


def test_long_history_1mb(client):
    msgs = [{"role": "user", "content": "가" * 1000}] * 350
    r = _post(client, {"messages": msgs, "candidate_answer": "답"})
    assert r.status_code == 200
    jsonschema.validate(r.json(), RESPONSE_SCHEMA)


_json = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(),
    lambda c: st.lists(c, max_size=4) | st.dictionaries(st.text(max_size=8), c, max_size=4),
    max_leaves=20,
)


@settings(
    max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(st.binary(max_size=256) | _json.map(lambda o: json.dumps(o).encode()))
def test_fuzz_always_200_and_schema(client, body):
    r = _post(client, body)
    assert r.status_code == 200
    jsonschema.validate(r.json(), RESPONSE_SCHEMA)


def test_trailing_slash_is_not_redirect(client):
    r = client.post("/guard/", content=b"{}", headers=AUTH, follow_redirects=False)
    assert not 300 <= r.status_code < 400


# --- 인증 ---


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": TOKEN},
        {"Authorization": "bearer " + TOKEN},
    ],
)
@pytest.mark.parametrize("path", ["/guard", "/tutor"])
def test_auth_failures_are_401_without_token(client, headers, path):
    r = _post(client, {"messages": [{"role": "user", "content": "q"}]}, headers=headers, path=path)
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}
    assert TOKEN not in r.text


def test_router_alone_without_setup_is_401():
    app = FastAPI()
    app.include_router(router)
    r = TestClient(app).post("/guard", content=b"{}", headers=AUTH)  # lifespan 없이
    assert r.status_code == 401


# --- 기동 ---


def test_startup_fails_without_team_token(monkeypatch):
    monkeypatch.delenv("TEAM_TOKEN")
    with pytest.raises(RuntimeError, match="TEAM_TOKEN"), TestClient(create_app(_constant)):
        pass


def test_startup_fails_with_empty_team_token(monkeypatch):
    monkeypatch.setenv("TEAM_TOKEN", "")
    with pytest.raises(RuntimeError, match="TEAM_TOKEN"), TestClient(create_app(_constant)):
        pass


def test_router_without_setup_fails_startup():
    app = FastAPI()
    app.include_router(router)
    with pytest.raises(RuntimeError, match="setup_guard"), TestClient(app):
        pass


def test_guard_lifespan_app_starts():
    app = FastAPI(lifespan=guard_lifespan, redirect_slashes=False)
    app.include_router(router)
    with TestClient(app) as c:
        assert _post(c, {"messages": []}).status_code == 200


def test_create_app_reads_guard_config(monkeypatch, tmp_path):
    raw = (
        (ROOT / "configs" / "default.yaml")
        .read_text("utf-8")
        .replace("budget_s: 20", "budget_s: 7")
    )
    path = tmp_path / "c.yaml"
    path.write_text(raw, encoding="utf-8")
    monkeypatch.setenv("GUARD_CONFIG", str(path))
    seen = []

    async def factory(cfg):
        seen.append(cfg)
        return ConstantGuard()

    with TestClient(create_app(factory)):
        pass
    assert [c.budget_s for c in seen] == [7]


def test_module_imports_without_env():
    assert isinstance(server.app, FastAPI)


# --- 복사한 /tutor ---


def test_tutor_has_no_client_until_first_call(client):
    assert getattr(client.app.state, "tutor_client", None) is None


def test_tutor_module_has_no_print():
    src = Path(server.__file__).read_text("utf-8")
    assert "print(" not in src and "logger.exception" not in src


def test_tutor_falls_back_without_openai_env(client, monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    r = _post(client, {"messages": [{"role": "user", "content": MARK}]}, path="/tutor")
    assert r.status_code == 200
    assert r.json() == {"answer": server.TUTOR_FALLBACK, "action": "answer", "citations": []}


# --- 예외·반사·로그 (GR-12) ---


def _boom(*_a, **_k):
    raise ValueError(f"요청 본문 {MARK}")


def _assert_logs_clean(caplog):
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert MARK not in text
    assert all(r.exc_info is None and r.exc_text is None for r in caplog.records)
    assert "Traceback" not in caplog.text and MARK not in caplog.text


def test_handler_exception_gives_default_and_clean_log(client, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(server, "parse_request", _boom)
    r = _post(client, {"messages": [{"role": "user", "content": MARK}], "candidate_answer": MARK})
    assert r.status_code == 200
    assert MARK not in r.text
    assert r.json() == {
        "input": {"decision": "allow", "category": "normal"},
        "output": {"grounded": True, "unsupported": [], "safe": True, "category": "normal"},
    }
    _assert_logs_clean(caplog)
    assert any("ValueError" in rec.getMessage() for rec in caplog.records)


def test_judge_exception_gives_default(client, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(ConstantGuard, "judge", _async_boom)
    r = _post(client, {"messages": [{"role": "user", "content": MARK}]})
    assert r.status_code == 200 and MARK not in r.text
    assert "output" not in r.json()
    _assert_logs_clean(caplog)


async def _async_boom(*_a, **_k):
    _boom()


def test_failure_default_ungrounded_is_used(monkeypatch, tmp_path):
    raw = (ROOT / "configs" / "default.yaml").read_text("utf-8")
    raw = raw.replace("failure_default: grounded", "failure_default: ungrounded")
    path = tmp_path / "c.yaml"
    path.write_text(raw, encoding="utf-8")
    monkeypatch.setenv("GUARD_CONFIG", str(path))
    monkeypatch.setattr(server, "parse_request", _boom)
    with TestClient(create_app(_constant)) as c:
        r = _post(c, b'{"candidate_answer": "x"}')
    assert r.json()["output"]["grounded"] is False


def test_middleware_exception_gives_default_and_clean_log(caplog):
    caplog.set_level(logging.DEBUG)
    app = create_app(_constant)

    @app.post("/boom")
    async def boom():
        _boom()

    with TestClient(app) as c:
        r = c.post("/boom", content=json.dumps({"x": MARK}).encode(), headers=AUTH)
    assert r.status_code == 200 and MARK not in r.text
    _assert_logs_clean(caplog)


def test_uvicorn_error_filter_strips_message_and_traceback(client, caplog):
    caplog.set_level(logging.DEBUG)
    log = logging.getLogger("uvicorn.error")
    try:
        _boom()
    except ValueError:
        log.exception("Exception in ASGI application %s", MARK)
    rec = [r for r in caplog.records if r.name == "uvicorn.error"][-1]
    assert rec.getMessage() == "exception ValueError"
    _assert_logs_clean(caplog)


def test_access_log_line_has_seven_fields(client, caplog):
    caplog.set_level(logging.INFO, logger="guardrail.access")
    _post(client, {"messages": [{"role": "user", "content": MARK}], "candidate_answer": MARK})
    lines = [r.getMessage() for r in caplog.records if r.name == "guardrail.access"]
    assert len(lines) == 1
    fields = lines[0].split(" ")
    assert len(fields) == 7
    _ts, path, req_b, resp_b, status, ms, err = fields
    assert (path, status, err) == ("/guard", "200", "-")
    assert int(req_b) > 0 and int(resp_b) > 0 and float(ms) >= 0
    assert MARK not in lines[0]


def test_access_log_hides_unknown_paths(client, caplog):
    caplog.set_level(logging.INFO, logger="guardrail.access")
    client.post(f"/{MARK}", content=b"{}", headers=AUTH)
    lines = [r.getMessage() for r in caplog.records if r.name == "guardrail.access"]
    assert lines and MARK not in lines[0] and lines[0].split(" ")[1] == "other"


@settings(
    max_examples=50, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(st.text(max_size=64))
def test_fuzz_no_reflection_in_logs_or_input(client, caplog, text):
    caplog.set_level(logging.DEBUG)
    marked = f"{MARK}{text}"
    r = _post(
        client, {"messages": [{"role": "user", "content": marked}], "candidate_answer": marked}
    )
    assert MARK not in json.dumps(r.json()["input"], ensure_ascii=False)
    assert MARK not in caplog.text


class _FakeCompletions:
    def __init__(self, content):
        self.content, self.calls = content, []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        msg = type("M", (), {"content": self.content})
        return type("R", (), {"choices": [type("C", (), {"message": msg})]})


class _FakeClient:
    def __init__(self, content):
        self.chat = type("Chat", (), {"completions": _FakeCompletions(content)})


class _SlotLLM:
    def __init__(self):
        self.entered = 0

    @contextlib.asynccontextmanager
    async def slot(self):
        self.entered += 1
        yield


def test_tutor_answers_through_llm_slot(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5-nano")
    llm = _SlotLLM()

    async def factory(cfg):
        g = ConstantGuard()
        g.llm = llm
        return g

    with TestClient(create_app(factory)) as c:
        fake = _FakeClient("  답변 " + "가" * 5000)
        c.app.state.tutor_client = fake
        r = _post(c, {"messages": [{"role": "user", "content": "q"}]}, path="/tutor")
    assert r.status_code == 200
    assert r.json()["answer"].startswith("답변") and len(r.json()["answer"]) == 4000
    assert llm.entered == 1
    sent = fake.chat.completions.calls[0]
    assert sent["messages"][1] == {"role": "user", "content": "q"}
    assert sent["messages"][0]["role"] == "system"


def test_tutor_empty_answer_falls_back(client, monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5-nano")
    client.app.state.tutor_client = _FakeClient(None)
    r = _post(client, {"messages": [{"role": "user", "content": "q"}]}, path="/tutor")
    assert r.json()["answer"] == server.TUTOR_FALLBACK


def test_not_found_does_not_reflect_path(client):
    r = client.post(f"/x{MARK}", content=b"{}", headers=AUTH)
    assert r.status_code == 404 and MARK not in r.text
