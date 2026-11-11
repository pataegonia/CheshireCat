"""HTTP 경로(TU-01·TU-12·TU-15): 가드 create_app에 주입한 POST /tutor."""

import asyncio
import importlib
import json
import logging
import time
import types

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.tutor.tutor_testlib import AUTH, COMPETITION, REPO, assert_contract, make_app
from tutor.runtime.response import CLARIFY_TEXT, TUTOR_DEFAULT

# tutor.router는 APIRouter(통합이 쓰는 이름)라서 모듈은 import_module로 가져온다
router_mod = importlib.import_module("tutor.router")

MARKER = "REFLECT-7c1e-마커"  # 응답·로그에 나오면 반사다
MIB = 1024 * 1024


def post(client, content: bytes, headers=AUTH):
    return client.post("/tutor", content=content, headers=headers)


def as_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode()


def access_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "guardrail.access"]


def assert_no_marker_in_logs(caplog) -> None:
    assert all(MARKER not in r.getMessage() for r in caplog.records)


def public_tutor_rows() -> list[dict]:
    lines = (COMPETITION / "public" / "tutor.jsonl").read_text("utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_public_tutor_rows_all_200_in_contract(client):
    rows = public_tutor_rows()
    assert len(rows) == 200
    for row in rows:
        body = {"spec_version": "1.0", "session_id": "s", "messages": row["messages"]}
        r = post(client, as_bytes(body))
        assert r.status_code == 200, row["id"]
        assert_contract(r.json())


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not json",
        b"[]",
        b"null",
        b'{"messages": 5}',
        b'{"messages": [{"role": "user"}]}',
        b"\xff\xfe" + MARKER.encode(),
        b"[" * 50_000 + b"]" * 50_000,
        ("{" + json.dumps(MARKER)).encode(),
    ],
)
def test_bad_bodies_get_default_200_without_reflection(client, content):
    r = post(client, content)
    assert r.status_code == 200
    assert r.json() == TUTOR_DEFAULT
    assert MARKER not in r.text


def test_long_history_about_1mb(client, caplog):
    caplog.set_level(logging.INFO, logger="guardrail.access")
    msgs = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "가" * 1000} for i in range(350)
    ]
    r = post(client, as_bytes({"messages": [*msgs, {"role": "user", "content": MARKER}]}))
    assert r.status_code == 200
    assert_contract(r.json())
    assert MARKER not in r.text
    assert access_lines(caplog)  # 성공 경로도 로그는 남고, 그 안에 요청 내용은 없다
    assert_no_marker_in_logs(caplog)


def test_chunked_body_without_content_length(client):
    def chunks():
        yield b'{"messages": [{"role": "user", '
        yield b'"content": "q"}]}'

    r = client.post("/tutor", content=chunks(), headers=AUTH)
    assert r.status_code == 200
    assert_contract(r.json())


def test_json_escaped_lone_surrogate_is_200(client):
    r = post(client, b'{"messages": [{"role": "user", "content": "\\ud800 q"}]}')
    assert r.status_code == 200
    assert_contract(r.json())


@pytest.mark.parametrize("content_type", [None, "application/json", "text/plain"])
def test_any_content_type_is_200(client, content_type):  # 통합 IN-05 계약 테스트
    headers = dict(AUTH) if content_type is None else {**AUTH, "Content-Type": content_type}
    r = post(client, as_bytes({"messages": [{"role": "user", "content": "q"}]}), headers)
    assert r.status_code == 200
    assert_contract(r.json())


def test_body_over_4mib_gets_default_and_logs_class_only(client, caplog):
    caplog.set_level(logging.INFO, logger="guardrail.access")
    big = as_bytes({"messages": [{"role": "user", "content": MARKER + "x" * (5 * 1024 * 1024)}]})
    r = post(client, big)
    assert r.status_code == 200
    assert r.json() == TUTOR_DEFAULT
    assert any(
        " /tutor " in line and line.endswith(" BodyTooLarge") for line in access_lines(caplog)
    )
    assert_no_marker_in_logs(caplog)


def test_handler_exception_gets_default_and_logs_class_only(client, caplog, monkeypatch):
    async def boom(body):
        raise ValueError(MARKER)

    monkeypatch.setattr(router_mod, "respond", boom)
    caplog.set_level(logging.INFO, logger="guardrail.access")
    r = post(client, as_bytes({"messages": [{"role": "user", "content": MARKER}]}))
    assert r.status_code == 200
    assert r.json() == TUTOR_DEFAULT
    assert any(line.endswith(" ValueError") for line in access_lines(caplog))
    assert_no_marker_in_logs(caplog)


def test_slow_handler_hits_the_deadline(monkeypatch, tmp_path, caplog):
    default_cfg = (REPO / "guardrail" / "configs" / "default.yaml").read_text("utf-8")
    assert "budget_s: 20" in default_cfg
    cfg = tmp_path / "guard.yaml"
    cfg.write_text(default_cfg.replace("budget_s: 20", "budget_s: 0.05"), "utf-8")
    monkeypatch.setenv("GUARD_CONFIG", str(cfg))
    monkeypatch.setattr(router_mod, "BACKSTOP_MARGIN_S", 0.0)

    async def slow(body):
        await asyncio.sleep(5)

    monkeypatch.setattr(router_mod, "respond", slow)
    caplog.set_level(logging.INFO, logger="guardrail.access")
    with TestClient(make_app()) as c:
        started = time.perf_counter()
        r = post(c, as_bytes({"messages": [{"role": "user", "content": "q"}]}))
        elapsed = time.perf_counter() - started
    assert r.status_code == 200
    assert r.json() == TUTOR_DEFAULT
    assert elapsed < 2
    assert any(line.endswith(" TimeoutError") for line in access_lines(caplog))


class _State:
    def __init__(self, budget):
        self.cfg = types.SimpleNamespace(budget_s=budget)


@pytest.mark.parametrize(
    ("budget", "expected"),
    [
        (20, "budget"),
        (100, "cap"),
        (0, "default"),
        (-1, "default"),
        (float("inf"), "default"),
        (float("nan"), "default"),
        (True, "default"),
        ("20", "default"),
        (None, "default"),
    ],
)
def test_deadline_is_budget_plus_margin_capped_under_30s(budget, expected):
    want = {
        "budget": 20 + router_mod.BACKSTOP_MARGIN_S,
        "cap": router_mod.DEADLINE_CAP_S,
        "default": router_mod.DEFAULT_BUDGET_S + router_mod.BACKSTOP_MARGIN_S,
    }[expected]
    assert router_mod.deadline_s(_State(budget)) == want
    assert router_mod.DEADLINE_CAP_S < 30


def test_deadline_without_cfg_uses_default_budget():
    want = router_mod.DEFAULT_BUDGET_S + router_mod.BACKSTOP_MARGIN_S
    assert router_mod.deadline_s(object()) == want


class _StreamingRequest:
    """read_body만 보는 가짜 요청: 조각을 하나씩 내주고 몇 조각을 읽었는지 센다."""

    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks
        self.consumed = 0
        self.state = types.SimpleNamespace()

    async def stream(self):
        for chunk in self._chunks:
            self.consumed += 1
            yield chunk


def test_read_body_reads_exactly_4mib():
    req = _StreamingRequest([b"x" * MIB] * 4)
    assert len(asyncio.run(router_mod.read_body(req))) == router_mod.MAX_BODY_BYTES == 4 * MIB
    assert req.state.req_bytes == 4 * MIB


def test_read_body_stops_right_after_the_cap():
    req = _StreamingRequest([b"x" * MIB] * 10)
    with pytest.raises(router_mod.BodyTooLarge):
        asyncio.run(router_mod.read_body(req))
    assert req.consumed == 5  # 상한을 넘긴 조각에서 멈춘다
    assert req.state.req_bytes == 5 * MIB  # 길이만 남는다


def test_guard_fallback_returns_tutor_default_unchanged(client, monkeypatch, caplog):
    """라우트 밖으로 예외가 나가도 가드 미들웨어가 defaults["/tutor"]를 200으로 낸다(2차 안전망)."""

    def broken_response(*args, **kwargs):
        raise RuntimeError(MARKER)

    monkeypatch.setattr(router_mod, "JSONResponse", broken_response)
    caplog.set_level(logging.INFO, logger="guardrail.access")
    r = post(client, as_bytes({"messages": [{"role": "user", "content": "q"}]}))
    assert r.status_code == 200
    assert r.json() == TUTOR_DEFAULT
    assert TUTOR_DEFAULT == {"answer": CLARIFY_TEXT, "action": "clarify", "citations": []}
    assert any(line.endswith(" RuntimeError") for line in access_lines(caplog))
    assert_no_marker_in_logs(caplog)


def test_trailing_slash_is_not_a_redirect(client):
    r = client.post("/tutor/", content=b"{}", headers=AUTH, follow_redirects=False)
    assert not 300 <= r.status_code < 400


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "test-token"}]
)
def test_auth_failures_are_401(client, headers):
    assert post(client, b"{}", headers).status_code == 401


@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(raw=st.binary(max_size=2000))
def test_fuzz_bytes_always_200_in_contract(client, raw):
    r = post(client, raw)
    assert r.status_code == 200
    assert_contract(r.json())


def test_default_constant_is_not_changed_by_requests(client):
    for content in (b"", as_bytes({"messages": [{"role": "user", "content": "q"}]})):
        post(client, content).json()["citations"].append("PY-01-01")
    assert TUTOR_DEFAULT == {"answer": CLARIFY_TEXT, "action": "clarify", "citations": []}
