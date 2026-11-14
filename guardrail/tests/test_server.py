"""S1.2 서버: 인증·기동·200 보장·반사 0·로그 규칙 (C1 §S1.2, GR-01·09·12, ADR-0009)."""

import asyncio
import contextlib
import dataclasses
import json
import logging

import hypothesis.strategies as st
import jsonschema
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from strategies import json_values

import guardrail.server as server
from guardrail.config import ROOT, GuardConfig
from guardrail.guard import ConstantGuard
from guardrail.schema import GuardRequest
from guardrail.server import create_app, guard_lifespan, router, setup_guard

TOKEN = "tok-123_abc"  # noqa: S105 — 테스트용 가짜 토큰
AUTH = {"Authorization": f"Bearer {TOKEN}"}
MARK = "MARKER-7f3a9c"
CONTRACT = json.loads((ROOT / "docs" / "contract" / "guard.schema.json").read_text("utf-8"))
RESPONSE_SCHEMA = {**CONTRACT, "$ref": "#/$defs/response"}
PUBLIC = ROOT.parent / "competition" / "public"
FUZZ = settings(
    max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
DEFAULT_WITH_OUTPUT = {
    "input": {"decision": "allow", "category": "normal"},
    "output": {"grounded": True, "unsupported": [], "safe": True, "category": "normal"},
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("GUARD_CONFIG", "GUARD_KB_DIR", "GUARD_PUBLIC_DIR", "GUARD_PROMPTS_DIR"):
        monkeypatch.delenv(name, raising=False)
    # 셸에 키가 있어도 기본 판정기가 실제 API를 부르지 않게 지운다(키가 필요한 테스트는 직접 넣는다)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("TEAM_TOKEN", TOKEN)


async def _constant(cfg: GuardConfig) -> ConstantGuard:
    return ConstantGuard()


def _factory(guard):
    async def factory(cfg):
        return guard

    return factory


@pytest.fixture
def client():
    with TestClient(create_app(_constant)) as c:
        yield c


def _post(client, body, headers=AUTH, path="/guard"):
    data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
    return client.post(path, content=data, headers=headers)


def _config_file(tmp_path, monkeypatch, old: str, new: str) -> None:
    raw = (ROOT / "configs" / "default.yaml").read_text("utf-8")
    assert old in raw
    path = tmp_path / "c.yaml"
    path.write_text(raw.replace(old, new), encoding="utf-8")
    monkeypatch.setenv("GUARD_CONFIG", str(path))


def _access_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "guardrail.access"]


def _assert_logs_clean(caplog):
    assert MARK not in caplog.text and "Traceback" not in caplog.text
    assert all(r.exc_info is None and r.exc_text is None for r in caplog.records)
    assert all(MARK not in r.getMessage() for r in caplog.records)


def _public_rows():
    rows = []
    for name in ("guard.jsonl", "tutor.jsonl"):
        lines = (PUBLIC / name).read_text("utf-8").splitlines()
        rows += [json.loads(line) for line in lines if line.strip()]
    return rows


class _RaisingGuard:
    """요청 내용을 예외 메시지에 담는 판정기(앞으로의 Guard가 그럴 수 있다)."""

    async def judge(self, req: GuardRequest):
        raise ValueError(" ".join(m.content for m in req.messages) + (req.candidate_answer or ""))


class _SlowGuard:
    async def judge(self, req: GuardRequest):
        await asyncio.sleep(5)


# --- 계약 ---


def test_public_rows_all_200_and_match_contract(client):
    rows = _public_rows()
    assert len(rows) == 400
    for row in rows:
        body = {"messages": row["messages"]}
        if "candidate_answer" in row:
            body["candidate_answer"] = row["candidate_answer"]
        r = _post(client, body)
        assert r.status_code == 200, row["id"]
        jsonschema.validate(r.json(), RESPONSE_SCHEMA)
        assert ("output" in r.json()) == ("candidate_answer" in row), row["id"]


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
    assert r.json() == DEFAULT_WITH_OUTPUT


def test_body_over_cap_gets_default_with_output(client, caplog):
    caplog.set_level(logging.INFO, logger="guardrail.access")
    r = _post(client, b"x" * (server.MAX_BODY_BYTES + 1))
    assert r.status_code == 200 and r.json() == DEFAULT_WITH_OUTPUT
    assert _access_lines(caplog)[-1].split(" ")[-1] == "BodyTooLarge"


@FUZZ
@given(st.binary(max_size=256) | json_values.map(lambda o: json.dumps(o).encode()))
def test_fuzz_always_200_and_schema(client, body):
    r = _post(client, body)
    assert r.status_code == 200
    jsonschema.validate(r.json(), RESPONSE_SCHEMA)


def test_trailing_slash_is_not_redirect(client):
    r = client.post("/guard/", content=b"{}", headers=AUTH, follow_redirects=False)
    assert not 300 <= r.status_code < 400


def test_get_guard_is_405_without_reflection(client):
    r = client.get(f"/guard?q={MARK}", headers=AUTH)
    assert r.status_code == 405 and MARK not in r.text


def test_not_found_does_not_reflect_path(client):
    r = client.post(f"/x{MARK}", content=b"{}", headers=AUTH)
    assert r.status_code == 404 and MARK not in r.text


# --- 인증 ---


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": TOKEN},
        {"Authorization": "bearer " + TOKEN},
        {"Authorization": f"Bearer {TOKEN} "},
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


def test_auth_failure_is_logged_by_class_name(client, caplog):
    caplog.set_level(logging.INFO, logger="guardrail.access")
    _post(client, b"{}", headers={})
    assert _access_lines(caplog)[-1].split(" ")[4:][::2] == ["401", "HTTPException"]


# --- 기동 ---


def test_log_filter_installed_at_import():
    assert any(
        isinstance(f, server._ExceptionNameOnly) for f in logging.getLogger("uvicorn.error").filters
    )


@pytest.mark.parametrize("value", [None, ""])
def test_startup_fails_without_team_token(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("TEAM_TOKEN")
    else:
        monkeypatch.setenv("TEAM_TOKEN", value)
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


def test_team_app_with_own_lifespan_calls_setup_guard():
    order = []

    @contextlib.asynccontextmanager
    async def team(app):
        order.append("team")
        await setup_guard(app, server.load_config())
        yield

    app = FastAPI(lifespan=team, redirect_slashes=False)
    app.include_router(router)
    with TestClient(app) as c:
        assert _post(c, b'{"candidate_answer": ""}').json() == DEFAULT_WITH_OUTPUT
    assert order == ["team"]


def test_create_app_reads_guard_config(monkeypatch, tmp_path):
    _config_file(tmp_path, monkeypatch, "budget_s: 20", "budget_s: 7")
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


def test_tutor_has_no_client_until_first_call(client):
    assert getattr(client.app.state, "tutor_client", None) is None


def test_tutor_falls_back_without_openai_env(client, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    r = _post(client, {"messages": [{"role": "user", "content": MARK}]}, path="/tutor")
    assert r.status_code == 200
    assert r.json() == {"answer": server.TUTOR_FALLBACK, "action": "answer", "citations": []}
    assert _access_lines(caplog)[-1].split(" ")[-1] == "ValueError"
    _assert_logs_clean(caplog)


@pytest.mark.parametrize("value", [None, "", "  "])
def test_tutor_blank_base_url_builds_no_client(client, monkeypatch, value):
    # 주소가 비면 SDK 기본(공개 OpenAI) 주소로 나간다. 만들지 않고 안내 문구 200
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5-nano")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    if value is None:
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("OPENAI_BASE_URL", value)
    built = []
    import openai

    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: built.append(kw))
    r = _post(client, {"messages": [{"role": "user", "content": "q"}]}, path="/tutor")
    assert r.status_code == 200 and r.json()["answer"] == server.TUTOR_FALLBACK
    assert built == []
    assert getattr(client.app.state, "tutor_client", None) is None


def test_tutor_client_uses_the_configured_base_url(client, monkeypatch):
    for k, v in {"OPENAI_MODEL": "gpt-5-nano", "OPENAI_API_KEY": "k"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("OPENAI_BASE_URL", " http://proxy.test/v1 ")
    built = []
    import openai

    def fake(**kw):
        built.append(kw)
        return _FakeClient("답")

    monkeypatch.setattr(openai, "AsyncOpenAI", fake)
    r = _post(client, {"messages": [{"role": "user", "content": "q"}]}, path="/tutor")
    assert r.json()["answer"] == "답"
    assert built[0]["base_url"] == "http://proxy.test/v1" and built[0]["api_key"] == "k"


def test_tutor_answers_through_llm_slot(monkeypatch):
    _sentinel_env(monkeypatch, OPENAI_MODEL="gpt-5-nano")
    llm = _SlotLLM()
    guard = ConstantGuard()
    guard.llm = llm
    with TestClient(create_app(_factory(guard))) as c:
        fake = _FakeClient("  답변 " + "가" * 5000)
        c.app.state.tutor_client = fake
        r = _post(c, {"messages": [{"role": "user", "content": "q"}]}, path="/tutor")
    assert r.status_code == 200
    assert r.json()["answer"].startswith("답변") and len(r.json()["answer"]) == 4000
    assert llm.entered == 1
    sent = fake.chat.completions.calls[0]
    assert sent["messages"][0]["role"] == "system"
    assert sent["messages"][1] == {"role": "user", "content": "q"}
    assert sent["max_completion_tokens"] == server.TUTOR_MAX_TOKENS


def test_tutor_empty_answer_falls_back(client, monkeypatch):
    _sentinel_env(monkeypatch, OPENAI_MODEL="gpt-5-nano")
    client.app.state.tutor_client = _FakeClient(None)
    r = _post(client, {"messages": [{"role": "user", "content": "q"}]}, path="/tutor")
    assert r.json()["answer"] == server.TUTOR_FALLBACK


# --- 예외·마감·반사·로그 (GR-09·12) ---


def _boom(*_a, **_k):
    raise ValueError(f"요청 본문 {MARK}")


def test_judge_exception_with_body_in_message_gives_default(caplog):
    caplog.set_level(logging.DEBUG)
    with TestClient(create_app(_factory(_RaisingGuard()))) as c:
        r = _post(c, {"messages": [{"role": "user", "content": MARK}], "candidate_answer": MARK})
    assert r.status_code == 200 and r.json() == DEFAULT_WITH_OUTPUT
    assert _access_lines(caplog)[-1].split(" ")[-1] == "ValueError"
    _assert_logs_clean(caplog)


def test_judge_exception_without_candidate_has_no_output():
    with TestClient(create_app(_factory(_RaisingGuard()))) as c:
        r = _post(c, {"messages": [{"role": "user", "content": MARK}]})
    assert r.json() == {"input": {"decision": "allow", "category": "normal"}}


def test_failure_default_ungrounded_is_used(monkeypatch, tmp_path):
    _config_file(tmp_path, monkeypatch, "failure_default: grounded", "failure_default: ungrounded")
    with TestClient(create_app(_factory(_RaisingGuard()))) as c:
        r = _post(c, b'{"candidate_answer": "x"}')
    assert r.json()["output"]["grounded"] is False


def test_slow_judge_hits_server_backstop(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="guardrail.access")
    _config_file(tmp_path, monkeypatch, "budget_s: 20", "budget_s: 0.05")
    monkeypatch.setattr(server, "BACKSTOP_MARGIN_S", 0.0)
    with TestClient(create_app(_factory(_SlowGuard()))) as c:
        r = _post(c, b'{"candidate_answer": "x"}')
    assert r.json() == DEFAULT_WITH_OUTPUT
    assert _access_lines(caplog)[-1].split(" ")[-1] == "TimeoutError"


def test_backstop_is_under_spec_limit():
    assert 20 + server.BACKSTOP_MARGIN_S < 30 and server.TUTOR_DEADLINE_S < 30


def test_middleware_exception_gives_default_and_clean_log(caplog):
    caplog.set_level(logging.DEBUG)
    app = create_app(_constant)

    @app.post("/boom")
    async def boom():
        _boom()

    with TestClient(app) as c:
        r = c.post("/boom", content=json.dumps({"x": MARK}).encode(), headers=AUTH)
    assert r.status_code == 200 and MARK not in r.text
    assert _access_lines(caplog)[-1].split(" ")[-1] == "ValueError"
    _assert_logs_clean(caplog)


def test_last_resort_when_default_body_fails(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="guardrail.access")
    monkeypatch.setattr(server, "_default_body", _boom)
    with TestClient(create_app(_factory(_RaisingGuard()))) as c:
        r = _post(c, {"messages": [{"role": "user", "content": MARK}]})
    assert r.status_code == 200 and r.json() == DEFAULT_WITH_OUTPUT
    assert len(_access_lines(caplog)) == 1


def test_uvicorn_error_filter_strips_exc_info(client, caplog):
    caplog.set_level(logging.DEBUG)
    try:
        _boom()
    except ValueError:
        logging.getLogger("uvicorn.error").exception("Exception in ASGI application %s", MARK)
    rec = [r for r in caplog.records if r.name == "uvicorn.error"][-1]
    assert rec.getMessage() == "exception ValueError"
    _assert_logs_clean(caplog)


def test_uvicorn_error_filter_strips_plain_traceback_text(caplog):
    caplog.set_level(logging.DEBUG)
    text = f'Traceback (most recent call last):\n  File "x.py"\nRuntimeError: {MARK}\n'
    logging.getLogger("uvicorn.error").error(text)
    rec = [r for r in caplog.records if r.name == "uvicorn.error"][-1]
    assert rec.getMessage() == "exception RuntimeError"
    _assert_logs_clean(caplog)


def test_access_log_line_has_seven_fields(client, caplog):
    caplog.set_level(logging.INFO, logger="guardrail.access")
    data = json.dumps({"messages": [{"role": "user", "content": MARK}]}).encode()
    _post(client, data)
    lines = _access_lines(caplog)
    assert len(lines) == 1
    fields = lines[0].split(" ")
    assert len(fields) == 7
    _ts, path, req_b, resp_b, status, ms, err = fields
    assert (path, req_b, status, err) == ("/guard", str(len(data)), "200", "-")
    assert int(resp_b) > 0 and float(ms) >= 0
    assert MARK not in lines[0]


def test_access_log_hides_unknown_paths(client, caplog):
    caplog.set_level(logging.INFO, logger="guardrail.access")
    client.post(f"/{MARK}", content=b"{}", headers=AUTH)
    lines = _access_lines(caplog)
    assert lines and MARK not in lines[0] and lines[0].split(" ")[1] == "other"


@FUZZ
@given(st.text(max_size=64))
def test_fuzz_no_reflection_with_echoing_guard(caplog, text):
    caplog.clear()
    caplog.set_level(logging.DEBUG)
    marked = f"{MARK}{text}"
    with TestClient(create_app(_factory(_RaisingGuard()))) as c:
        r = _post(
            c, {"messages": [{"role": "user", "content": marked}], "candidate_answer": marked}
        )
    assert r.status_code == 200 and MARK not in r.text
    _assert_logs_clean(caplog)


# --- 팀 조립: create_app 주입 인자 (PR #92 리뷰, TU-01·IN-05) ---

TUTOR_DEFAULT = {"answer": "팀 기본 문구", "action": "answer", "citations": []}


def _team_tutor_router(fail: bool = False):
    from fastapi import APIRouter
    from pydantic import BaseModel

    class Body(BaseModel):
        messages: list[dict]

    r = APIRouter()

    @r.post("/tutor")
    async def team_tutor(body: Body) -> dict:
        if fail:
            raise RuntimeError(MARK)
        return {"answer": "팀 튜터", "action": "answer", "citations": []}

    return r


def _post_json(client, body, headers=AUTH):
    return client.post("/tutor", json=body, headers=headers)  # 팀 라우트는 content-type을 본다


def _team_app(**kw):
    return create_app(_constant, defaults={"/tutor": TUTOR_DEFAULT}, **kw)


@pytest.mark.parametrize(("headers", "status"), [({}, 401), (AUTH, 200)])
def test_injected_tutor_router_is_behind_guard_auth(headers, status):
    with TestClient(_team_app(tutor_router=_team_tutor_router())) as c:
        r = _post_json(c, {"messages": []}, headers=headers)
    assert r.status_code == status
    if status == 200:
        assert r.json()["answer"] == "팀 튜터"


def test_injected_tutor_exception_gives_team_default(caplog):
    with TestClient(_team_app(tutor_router=_team_tutor_router(fail=True))) as c:
        r = _post_json(c, {"messages": []})
    assert (r.status_code, r.json()) == (200, TUTOR_DEFAULT)
    _assert_logs_clean(caplog)


def test_injected_tutor_validation_error_is_not_422(caplog):
    with TestClient(_team_app(tutor_router=_team_tutor_router())) as c:
        r = _post_json(c, {"messages": MARK})
    assert (r.status_code, r.json()) == (200, TUTOR_DEFAULT)
    assert MARK not in r.text
    _assert_logs_clean(caplog)


def test_startups_run_once_after_setup():
    calls = []

    async def build_index(state):
        calls.append(state.guard is not None and state.team_token == TOKEN)

    with TestClient(_team_app(startups=(build_index,))) as c:
        _post(c, {"messages": []})
        _post(c, {"messages": []})
    assert calls == [True]


def test_no_injection_keeps_starter_tutor(client, monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    r = _post(client, {"messages": []}, path="/tutor")
    assert r.json()["answer"] == server.TUTOR_FALLBACK


SENT_KEY, SENT_MODEL, SENT_URL = (
    "SENTINEL-KEY-7f3",
    "SENTINEL-MODEL-7f3",
    "http://SENTINEL-URL-7f3/v1",
)


def _sentinel_env(monkeypatch, **override):
    base = {"OPENAI_API_KEY": SENT_KEY, "OPENAI_MODEL": SENT_MODEL, "OPENAI_BASE_URL": SENT_URL}
    for k, v in {**base, **override}.items():
        monkeypatch.setenv(k, v)


@pytest.mark.parametrize("name", ["OPENAI_API_KEY", "OPENAI_MODEL"])
def test_tutor_blank_key_or_model_builds_no_client(client, monkeypatch, name):
    _sentinel_env(monkeypatch, **{name: "  "})
    built = []
    import openai

    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: built.append(kw))
    r = _post(client, {"messages": [{"role": "user", "content": "q"}]}, path="/tutor")
    assert r.status_code == 200 and r.json()["answer"] == server.TUTOR_FALLBACK
    assert built == []
    assert getattr(client.app.state, "tutor_client", None) is None


def test_tutor_logs_and_errors_hide_env_values(client, monkeypatch, caplog):
    import httpx2
    import openai

    _sentinel_env(monkeypatch)
    caplog.set_level(logging.DEBUG)

    class _Leaky:
        def __init__(self, **kw):
            self.chat = type("Chat", (), {"completions": self})

        async def create(self, **kw):
            # 실제 HTTP 클라이언트의 로그 경로(httpx2 로거)를 네트워크 없이 지나가게 한다
            transport = httpx2.MockTransport(lambda request: httpx2.Response(200))
            async with httpx2.AsyncClient(transport=transport) as http:
                await http.post(SENT_URL + "/chat/completions")
            raise RuntimeError(f"{SENT_KEY} {SENT_MODEL} {SENT_URL}")

    monkeypatch.setattr(openai, "AsyncOpenAI", _Leaky)
    r = _post(client, {"messages": [{"role": "user", "content": "q"}]}, path="/tutor")
    assert r.status_code == 200 and r.json()["answer"] == server.TUTOR_FALLBACK
    assert any(rec.name == "guardrail.access" for rec in caplog.records)
    for sentinel in ("SENTINEL-KEY-7f3", "SENTINEL-MODEL-7f3", "SENTINEL-URL-7f3"):
        # URL 호스트는 HTTP 클라이언트가 소문자로 바꿔 찍으므로 대소문자 없이 본다
        assert sentinel.lower() not in r.text.lower()
        assert sentinel.lower() not in caplog.text.lower()


@pytest.fixture
def root_debug():
    root = logging.getLogger()
    old = root.level
    root.setLevel(logging.DEBUG)
    yield
    root.setLevel(old)


@pytest.mark.parametrize("debug_root", [False, True])
def test_http_client_loggers_stay_at_warning(debug_root, request):
    if debug_root:
        request.getfixturevalue("root_debug")
    server._install_log_filters()
    for name in ("httpx2", "httpcore2", "httpx"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING


@pytest.mark.parametrize("name", ["OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL"])
def test_injected_tutor_client_still_needs_all_three_env(client, monkeypatch, caplog, name):
    import openai

    _sentinel_env(monkeypatch, **{name: "  "})
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: pytest.fail("client built"))
    fake = _FakeClient("답")
    client.app.state.tutor_client = fake
    caplog.set_level(logging.DEBUG)
    r = _post(client, {"messages": [{"role": "user", "content": "q"}]}, path="/tutor")
    assert r.status_code == 200
    assert r.json()["answer"] == server.TUTOR_FALLBACK
    assert fake.chat.completions.calls == []
    assert _access_lines(caplog)[-1].split(" ")[-1] == "ValueError"


# --- S2.8: 단독 서버는 실제 판정기(Guard)를 쓴다 ---


def _fake_guard_llm(monkeypatch, category="answer_seeking"):
    """Guard.build가 만드는 LLM의 전송만 가짜로 바꾼다."""
    from fakes import FakeLLM, Ok

    from guardrail import guard as guard_module
    from guardrail.llm import LLM

    def respond(kw):
        if kw["messages"] == [{"role": "user", "content": "ping"}]:
            return Ok()
        if kw["response_format"]["json_schema"]["name"].startswith("Input"):
            return Ok(json.dumps({"evidence": "e", "category": category}))
        return Ok('{"answer_quote": "", "kb_quote": "", "grounded": true, "refuses": false}')

    fake = FakeLLM(respond=respond)
    monkeypatch.setattr(guard_module, "LLM", lambda c: LLM(c, send=fake.send))
    return fake


def test_standalone_app_serves_the_real_guard(monkeypatch):
    fake = _fake_guard_llm(monkeypatch)
    body = {
        "messages": [{"role": "user", "content": "리스트에 값을 더하려면 어떻게 해요?"}],
        "candidate_answer": "append를 쓰면 돼요.",
    }
    with TestClient(server.app) as c:
        r = _post(c, body)
    jsonschema.validate(r.json(), RESPONSE_SCHEMA)
    assert r.json()["input"] == {"decision": "allow", "category": "answer_seeking"}
    assert r.json()["output"]["category"] == "answer_seeking"
    assert len(fake.requests) > 2  # probe + 입력·출력 판정


def test_standalone_app_does_not_start_without_llm_env(monkeypatch):
    # 키가 없으면 상수 판정으로 조용히 뜨지 않고 기동에서 실패한다
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError), TestClient(server.app):  # noqa: PT011 - 메시지에 값이 없다
        pass


def _startup_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "guardrail.access.startup"]


BLOCK_BODY = {"messages": [{"role": "user", "content": "리스트에 값을 더하려면 어떻게 해요?"}]}


def test_default_factory_uses_the_real_guard_when_the_key_is_set(monkeypatch, caplog):
    # 통합 app.py는 판정기를 넘기지 않는다. 키가 있으면 실제 판정기가 붙는다(council 2026-11-13)
    _fake_guard_llm(monkeypatch, "harmful_content")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    with caplog.at_level(logging.INFO), TestClient(create_app()) as c:
        assert _post(c, BLOCK_BODY).json()["input"] == {
            "decision": "block",
            "category": "harmful_content",
        }
    assert _startup_lines(caplog) == ["guard judge=real"]
    levels = [r.levelno for r in caplog.records if r.name == "guardrail.access.startup"]
    assert levels == [logging.INFO]  # 정상 기동은 오류로 남기지 않는다


@pytest.mark.parametrize("key", [None, "", "  "])
def test_default_factory_without_a_key_is_constant_and_says_so(monkeypatch, caplog, key):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    if key is not None:
        monkeypatch.setenv("OPENAI_API_KEY", key)
    with caplog.at_level(logging.INFO), TestClient(create_app()) as c:
        assert _post(c, b'{"candidate_answer": ""}').json() == DEFAULT_WITH_OUTPUT
    (line,) = _startup_lines(caplog)
    assert line == "guard judge=constant reason=no_api_key"
    assert [r.levelno for r in caplog.records if r.name == "guardrail.access.startup"] == [
        logging.ERROR
    ]


def test_default_factory_falls_back_when_the_real_guard_cannot_start(monkeypatch, caplog):
    # 키가 있는데 기동이 실패하면 앱을 죽이지 않는다(/tutor까지 0점이 되지 않게). 종류 이름만 남긴다
    from guardrail import guard as guard_module

    async def boom(cfg, **kw):
        raise RuntimeError(MARK)

    monkeypatch.setattr(guard_module.Guard, "build", boom)
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    with caplog.at_level(logging.INFO), TestClient(create_app()) as c:
        assert _post(c, BLOCK_BODY).json()["input"] == {"decision": "allow", "category": "normal"}
    assert _startup_lines(caplog) == ["guard judge=constant reason=RuntimeError"]
    _assert_logs_clean(caplog)


def test_default_factory_reports_a_guard_that_cannot_be_built(monkeypatch, caplog, tmp_path):
    # 프롬프트 경로가 틀리면 "실제 판정기"로 뜨지 않는다(기본값만 내게 되는 것을 로그로 남긴다)
    _fake_guard_llm(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("GUARD_PROMPTS_DIR", str(tmp_path / "none"))
    with caplog.at_level(logging.INFO), TestClient(create_app()) as c:
        assert _post(c, BLOCK_BODY).status_code == 200
    assert _startup_lines(caplog) == ["guard judge=constant reason=FileNotFoundError"]


def test_default_factory_reports_a_failed_probe(monkeypatch, caplog):
    from fakes import FakeLLM, Timeout

    from guardrail import guard as guard_module
    from guardrail.llm import LLM

    fake = FakeLLM(respond=lambda kw: Timeout())
    monkeypatch.setattr(guard_module, "LLM", lambda c: LLM(c, send=fake.send))
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    with caplog.at_level(logging.INFO), TestClient(create_app()):
        pass
    records = [r for r in caplog.records if r.name == "guardrail.access.startup"]
    assert [(r.getMessage(), r.levelno) for r in records] == [
        ("guard judge=real probe=timeout", logging.ERROR)
    ]


class _DegradedGuard:
    """200으로 답하지만 안에서 기본값으로 떨어진 판정(S2.10: 부하 시험이 이 비율을 센다)."""

    async def judge(self, req: GuardRequest):
        ok = await ConstantGuard().judge(req)
        return dataclasses.replace(ok, errors=("input_judge", "output_judge:TimeoutError"))


def test_judgement_error_codes_reach_access_log(caplog):
    caplog.set_level(logging.DEBUG)
    with TestClient(create_app(_factory(_DegradedGuard()))) as c:
        r = _post(c, {"messages": [{"role": "user", "content": MARK}], "candidate_answer": MARK})
    assert r.status_code == 200
    fields = _access_lines(caplog)[-1].split(" ")
    assert len(fields) == 7 and fields[-1] == "input_judge,output_judge:TimeoutError"
    _assert_logs_clean(caplog)


def test_guard_lifespan_and_setup_guard_default_to_the_default_factory(caplog):
    # 팀 앱이 판정기를 넘기지 않으면 기본 판정기(default_factory)다. 키가 없으니 상수로 뜬다
    app = FastAPI(lifespan=guard_lifespan, redirect_slashes=False)
    with caplog.at_level(logging.INFO), TestClient(app):
        pass
    assert _startup_lines(caplog) == ["guard judge=constant reason=no_api_key"]


def test_constant_factory_gives_the_constant_judge():
    assert isinstance(asyncio.run(server.constant_factory(server.load_config())), ConstantGuard)
