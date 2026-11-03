"""S0.3 LLM 게이트웨이: 오류 분류·재시도·게이트(AIMD)·마감·예산·faults·slot·구조화 출력.

시간은 FakeClock으로 다룬다. 실제 sleep·네트워크에 기대는 테스트는 없다(live 표시 1개 제외).
"""

import asyncio
import email.utils
import json
import os

import httpx2
import pytest
from fakes import (
    BadRequest,
    FakeClock,
    FakeLLM,
    Malformed,
    Ok,
    RateLimited,
    ServerError,
    Timeout,
    fake_config,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict

from guardrail import Guard
from guardrail.llm import (
    FAKE_OK,
    LLM,
    BudgetExceeded,
    Gate,
    TransportError,
    classify,
    openai_send,
    retry_wait,
)

pytestmark = pytest.mark.anyio

MSG = [{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}]
ERRORS = {"timeout", "rate_limit", "server", "bad_json", "auth", "bad_request", "budget", None}


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool


# --- 오류 분류 ---


@pytest.mark.parametrize(
    ("err", "expected"),
    [
        (TransportError("timeout"), ("timeout", True)),
        (TransportError("status", 408), ("timeout", True)),
        (TransportError("connect"), ("server", True)),
        (TransportError("status", 409), ("server", True)),
        (TransportError("status", 503), ("server", True)),
        (TransportError("status", 429), ("rate_limit", True)),
        (TransportError("status", 401), ("auth", False)),
        (TransportError("status", 403), ("auth", False)),
        (TransportError("status", 400), ("bad_request", False)),
        (TransportError("status", 404), ("bad_request", False)),
        (TransportError("status", 422), ("bad_request", False)),
        (TransportError("status", 500, {"x-should-retry": "false"}), ("server", False)),
        (TransportError("status", 400, {"x-should-retry": "true"}), ("bad_request", True)),
    ],
)
def test_classify(err, expected):
    assert classify(err) == expected


def test_transport_error_carries_no_message():
    e = TransportError("status", 500, {"a": "b"})
    assert str(e) == "" and e.args == ()


# --- retry_wait (openai-python 3.42.1 규칙, master §4.3 v18) ---


def test_retry_wait_prefers_ms_header():
    assert retry_wait({"retry-after-ms": "250", "retry-after": "9"}, 0, lambda: 0.0, 0.0) == 0.25


def test_retry_wait_seconds_and_http_date():
    assert retry_wait({"retry-after": "1.5"}, 0, lambda: 0.0, 0.0) == 1.5
    now = 1_700_000_000.0
    date = email.utils.formatdate(now + 7, usegmt=True)
    assert retry_wait({"retry-after": date}, 0, lambda: 0.0, now) == pytest.approx(7, abs=1)


def test_retry_wait_honors_up_to_120s_and_refuses_beyond():
    assert retry_wait({"retry-after": "90"}, 0, lambda: 0.0, 0.0) == 90
    assert retry_wait({"retry-after": "120"}, 0, lambda: 0.0, 0.0) == 120
    assert retry_wait({"retry-after": "130"}, 0, lambda: 0.0, 0.0) is None
    assert (
        retry_wait({"retry-after": "1e400"}, 0, lambda: 0.0, 0.0) is None
    )  # 수치 넘침 = 과도한 대기


@given(
    n=st.integers(0, 4),
    r=st.floats(0, 0.999),
    headers=st.sampled_from(
        [{}, {"retry-after": "0"}, {"retry-after": "abc"}, {"retry-after": "-3"}]
    ),
)
def test_retry_wait_backoff_with_jitter(n, r, headers):
    b = min(0.5 * 2**n, 8)
    wait = retry_wait(headers, n, lambda: r, 0.0)
    assert 0.75 * b < wait <= b


def test_retry_wait_backoff_caps_at_8():
    assert retry_wait({}, 10, lambda: 0.0, 0.0) == 8


# --- complete: 재시도·마감 ---


async def test_rate_limited_then_ok_waits_retry_after():
    llm = FakeLLM(script=[RateLimited(1.5), Ok("hi")])
    res = await llm.complete("generator", MSG, deadline_s=20.0)
    assert (res.text, res.error, res.attempts) == ("hi", None, 2)
    assert llm.clock.slept == [1.5]
    assert llm.usage["generator"].retries == 1


async def test_retry_after_longer_than_deadline_fails_without_waiting():
    llm = FakeLLM(script=[RateLimited(30)])
    start = llm.clock.now()
    res = await llm.complete("input_judge", MSG, deadline_s=3.0)
    assert res.error == "rate_limit" and res.attempts == 1
    assert llm.clock.now() == start and llm.clock.slept == []


async def test_retry_after_over_cap_is_not_retried():
    llm = FakeLLM(script=[RateLimited(130)])
    res = await llm.complete("generator", MSG, deadline_s=500.0)
    assert res.error == "rate_limit" and res.attempts == 1 and llm.clock.slept == []


async def test_three_server_errors_stop_at_three_attempts():
    llm = FakeLLM(script=[ServerError(), ServerError(), ServerError(), Ok("never")])
    res = await llm.complete("generator", MSG, deadline_s=20.0)
    assert (res.error, res.attempts) == ("server", 3)
    assert len(llm.requests) == 3
    assert llm.usage["generator"].errors["server"] == 1


async def test_auth_error_not_retried():
    llm = FakeLLM(script=[ServerError(401)])
    res = await llm.complete("generator", MSG, deadline_s=20.0)
    assert (res.error, res.attempts) == ("auth", 1)


async def test_timeout_is_classified():
    res = await FakeLLM(script=[Timeout(), Timeout(), Timeout()]).complete(
        "generator", MSG, deadline_s=20.0
    )
    assert res.error == "timeout" and res.attempts == 3


async def test_deadline_counts_send_time_and_passes_remaining():
    clock = FakeClock()
    seen: list[float] = []

    def responder(req):
        seen.append(req["timeout"])
        clock.advance(1.0)  # 보내는 데 1초
        return ServerError() if len(seen) == 1 else Ok("ok")

    llm = FakeLLM(responder=responder, clock=clock)
    res = await llm.complete("input_judge", MSG, deadline_s=2.5)
    assert res.error is None
    assert seen[0] == 2.5
    assert seen[1] == pytest.approx(2.5 - 1.0 - 0.5)  # 보낸 시간과 백오프 0.5s만 뺀다


async def test_retry_budget_is_10_percent_with_floor_5():
    llm = FakeLLM(responder=lambda req: ServerError())
    # 100호출이 모두 실패: 재시도 예산 max(5, 0.1 × 100) = 10에서 멈춘다
    results = [await llm.complete("generator", MSG, deadline_s=20.0) for _ in range(100)]
    assert llm.usage["generator"].retries == 10
    assert sum(r.attempts for r in results) == 110
    assert all(r.error == "server" for r in results)


async def test_retry_budget_floor_allows_five():
    llm = FakeLLM(script=[ServerError(), Ok("a")] * 6)
    for _ in range(6):
        await llm.complete("generator", MSG, deadline_s=20.0)
    assert llm.usage["generator"].retries == 5  # 첫 시도 6회 → max(5, 0.6) = 5


# --- 예산 ---


async def test_budget_blocks_third_call_without_sending():
    llm = FakeLLM(script=[Ok("a"), Ok("b"), Ok("c")], max_calls=2)
    for _ in range(2):
        assert (await llm.complete("generator", MSG, deadline_s=20.0)).error is None
    res = await llm.complete("generator", MSG, deadline_s=20.0)
    assert res.error == "budget" and res.attempts == 0
    assert len(llm.requests) == 2
    assert llm.usage["generator"].errors["budget"] == 1


async def test_slot_and_complete_share_budget():
    llm = FakeLLM(script=[Ok("a"), Ok("b")], max_calls=2)
    async with llm.slot("generator"):
        pass
    assert (await llm.complete("input_judge", MSG, deadline_s=2.5)).error is None
    res = await llm.complete("input_judge", MSG, deadline_s=2.5)
    assert res.error == "budget" and len(llm.requests) == 1
    with pytest.raises(BudgetExceeded):
        async with llm.slot("generator"):
            pass
    assert llm.usage["generator"].errors["budget"] == 1
    assert llm.gate.in_flight == 0


# --- 구조화 출력 ---


async def test_malformed_then_ok_reasks_once_and_uses_reask_finish_reason():
    llm = FakeLLM(script=[Malformed(), Ok('{"ok": true}', finish_reason="stop")])
    res = await llm.complete("attack_judge", MSG, schema=Out, deadline_s=10.0)
    assert res.parsed == Out(ok=True) and res.error is None and res.finish_reason == "stop"
    assert res.attempts == 2 and llm.usage["attack_judge"].reasks == 1
    reask = llm.requests[1]["messages"]
    assert reask[-2] == {"role": "assistant", "content": "JSON 아님"}
    assert "JSON 객체 하나만" in reask[-1]["content"]


async def test_malformed_twice_is_bad_json_with_raw_text():
    llm = FakeLLM(script=[Malformed("x1"), Malformed("x2")])
    res = await llm.complete("attack_judge", MSG, schema=Out, deadline_s=10.0)
    assert (res.error, res.text, res.parsed) == ("bad_json", "x2", None)


async def test_json_schema_bad_request_downgrades_to_json_object_and_stays():
    llm = FakeLLM(script=[BadRequest(), Ok('{"ok": false}'), Ok('{"ok": true}')])
    res = await llm.complete("attack_judge", MSG, schema=Out, deadline_s=10.0)
    assert res.parsed == Out(ok=False)
    assert llm.requests[0]["response_format"]["type"] == "json_schema"
    assert llm.requests[0]["response_format"]["json_schema"]["strict"] is True
    assert llm.requests[1]["response_format"] == {"type": "json_object"}
    assert "JSON 스키마" in llm.requests[1]["messages"][0]["content"]
    await llm.complete("attack_judge", MSG, schema=Out, deadline_s=10.0)
    assert llm.requests[2]["response_format"] == {"type": "json_object"}  # 이 인스턴스에서 유지


async def test_bad_request_without_schema_is_not_downgraded():
    llm = FakeLLM(script=[BadRequest()])
    res = await llm.complete("generator", MSG, deadline_s=20.0)
    assert res.error == "bad_request" and len(llm.requests) == 1


async def test_prompt_level_extracts_braces_and_adds_system_message():
    llm = FakeLLM(
        fake_config(**{"gate.structured_output": "prompt"}),
        script=[Ok('판정: {"ok": true} 끝')],
    )
    res = await llm.complete(
        "attack_judge", [{"role": "user", "content": "q"}], schema=Out, deadline_s=10.0
    )
    assert res.parsed == Out(ok=True)
    req = llm.requests[0]
    assert req["response_format"] is None
    assert req["messages"][0]["role"] == "system" and "JSON 스키마" in req["messages"][0]["content"]


async def test_prompt_level_bottom_level_bad_request_is_final():
    llm = FakeLLM(fake_config(**{"gate.structured_output": "prompt"}), script=[BadRequest()])
    res = await llm.complete("attack_judge", MSG, schema=Out, deadline_s=10.0)
    assert res.error == "bad_request"


# --- 샘플링 상수·max_tokens·집계 ---


@pytest.mark.parametrize(
    ("role", "temp", "mt"),
    [
        ("generator", 0.7, 512),
        ("input_judge", 0.0, 384),
        ("exchange_judge", 0.0, 256),
        ("attack_judge", 0.0, 256),
    ],
)
async def test_role_sampling_constants(role, temp, mt):
    llm = FakeLLM(script=[Ok("x")])
    await llm.complete(role, MSG, deadline_s=5.0)
    assert (llm.requests[0]["temperature"], llm.requests[0]["max_tokens"]) == (temp, mt)


async def test_max_tokens_override():
    llm = FakeLLM(script=[Ok("x")])
    await llm.complete("generator", MSG, deadline_s=5.0, max_tokens=64)
    assert llm.requests[0]["max_tokens"] == 64


async def test_tokens_latency_and_usage_missing():
    llm = FakeLLM(script=[ServerError(), Ok("a", 7, 3, "length"), Ok("b", None, None)])
    res = await llm.complete("generator", MSG, deadline_s=20.0)
    assert (res.tokens_in, res.tokens_out, res.finish_reason) == (7, 3, "length")
    assert res.latency_ms >= 0
    res2 = await llm.complete("generator", MSG, deadline_s=20.0)
    assert (res2.tokens_in, res2.tokens_out) == (0, 0)
    u = llm.usage["generator"]
    assert u.usage_missing == 1 and u.tokens_in == 7 and u.calls == 3 and len(u.latencies_ms) == 2


async def test_missing_model_name_fails_without_sending():
    llm = FakeLLM(fake_config(**{"models.generator": None}))
    res = await llm.complete("generator", MSG, deadline_s=5.0)
    assert res.error == "bad_request" and llm.requests == []


# --- faults ---


@pytest.mark.parametrize("fault", ["timeout", "rate_limit", "server", "bad_json"])
async def test_fault_values_return_error_without_sending(fault):
    llm = FakeLLM(fake_config(**{"faults.exchange_judge": fault}))
    res = await llm.complete("exchange_judge", MSG, deadline_s=3.0)
    assert (res.error, res.attempts, res.tokens_in, res.finish_reason) == (fault, 0, 0, None)
    assert llm.requests == [] and llm.gate.in_flight == 0
    assert llm.usage["exchange_judge"].faults == 1 and llm.usage["exchange_judge"].calls == 0


async def test_fake_ok_with_schema_parses_without_sending():
    llm = FakeLLM(fake_config(**{"faults.attack_judge": "fake_ok"}), max_calls=0)
    res = await llm.complete("attack_judge", MSG, schema=AttackOut, deadline_s=10.0)
    assert res.parsed == AttackOut(refused=False, success=False, reason="fake")
    assert res.error is None and res.attempts == 0 and llm.requests == []


class AttackOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refused: bool
    success: bool
    reason: str


async def test_faults_apply_per_role():
    cfg = fake_config(faults={"generator": "fake_ok", "exchange_judge": "timeout"})
    llm = FakeLLM(cfg, script=[Ok("real")])
    user = {
        "role": "user",
        "content": "[자료]\n[S1] (sci-01 융해) 얼음이 녹는다. 물이 된다.\n\n[질문]\nq",
    }
    gen = await llm.complete("generator", [user], deadline_s=20.0)
    ex = await llm.complete("exchange_judge", MSG, deadline_s=3.0)
    judge = await llm.complete("input_judge", MSG, deadline_s=2.5)
    assert gen.text == "얼음이 녹는다. [S1]"
    assert ex.error == "timeout" and judge.text == "real"
    assert [r["role"] for r in llm.requests] == ["input_judge"]


def test_fake_ok_registry_has_gp0_roles():
    assert set(FAKE_OK) >= {"generator", "attack_judge"}
    user = {
        "role": "user",
        "content": "[자료]\n[S1] (출처) 첫 문장이다! 둘째.\n[S2] (b) 다른 청크.\n\n[질문]\nq",
    }
    assert FAKE_OK["generator"]([user]) == "첫 문장이다! [S1]"
    assert json.loads(FAKE_OK["attack_judge"]([user])) == {
        "refused": False,
        "success": False,
        "reason": "fake",
    }


def test_fake_generator_without_chunks_says_not_found():
    assert (
        FAKE_OK["generator"]([{"role": "user", "content": "[질문]\nq"}])
        == "교재에서 찾지 못했어요."
    )


def test_unregistered_fake_ok_role_is_config_error():
    with pytest.raises(ValueError, match="exchange_judge"):
        LLM(fake_config(**{"faults.exchange_judge": "fake_ok"}))


async def test_custom_fake_responders():
    def f(messages):
        return "custom"

    cfg = fake_config(**{"faults.exchange_judge": "fake_ok"})
    llm = LLM(cfg, fake_responders={"exchange_judge": f}, send=_never_send)
    assert (await llm.complete("exchange_judge", MSG, deadline_s=3.0)).text == "custom"


async def _never_send(*a, **kw):
    raise AssertionError("보내면 안 된다")


def test_llm_does_not_import_eval():
    import ast

    import guardrail.llm as mod

    tree = ast.parse(open(mod.__file__, encoding="utf-8").read())
    names = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert not [n for n in names if n.startswith("guardrail.eval") or n == "fakes"]


async def test_env_missing_names_variable_only(monkeypatch):
    monkeypatch.delenv("GUARD_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("GUARD_LLM_API_KEY", raising=False)
    llm = LLM(fake_config())
    with pytest.raises(RuntimeError, match="GUARD_LLM_BASE_URL"):
        await llm.complete("generator", MSG, deadline_s=1.0)


async def test_all_roles_faked_needs_no_env(monkeypatch):
    monkeypatch.delenv("GUARD_LLM_BASE_URL", raising=False)
    cfg = fake_config(faults={"generator": "fake_ok", "attack_judge": "fake_ok"})
    llm = LLM(cfg)
    assert (await llm.complete("generator", MSG, deadline_s=1.0)).error is None


# --- Gate · AIMD ---


async def test_gate_caps_concurrency():
    active = peak = 0

    def responder(req):
        return Ok("x")

    llm = FakeLLM(fake_config(**{"gate.initial": 2}), responder=responder)
    orig = llm._send

    async def tracking(*a, **kw):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        active -= 1
        return await orig(*a, **kw)

    llm._send = tracking
    await asyncio.gather(*(llm.complete("generator", MSG, deadline_s=5.0) for _ in range(8)))
    assert peak == 2 and llm.gate.in_flight == 0


def test_gate_halves_once_per_epoch_on_429():
    async def run():
        g = Gate(initial=4, min_=1, max_=16, increase_after=10)
        e1 = await g.acquire(1.0)
        e2 = await g.acquire(1.0)
        g.release(e1, "rate_limit")
        g.release(e2, "rate_limit")  # 같은 구간의 둘째 429는 무시
        return g.limit, g.in_flight

    assert asyncio.run(run()) == (2, 0)


def test_gate_increases_after_10_successes_and_respects_bounds():
    async def run():
        g = Gate(initial=4, min_=1, max_=5, increase_after=10)
        for _ in range(25):
            g.release(await g.acquire(1.0), "ok")
        g2 = Gate(initial=1, min_=1, max_=4, increase_after=10)
        g2.release(await g2.acquire(1.0), "rate_limit")
        return g.limit, g2.limit

    assert asyncio.run(run()) == (5, 1)


def test_gate_other_outcomes_reset_streak_only():
    async def run():
        g = Gate(initial=4, min_=1, max_=16, increase_after=10)
        for _ in range(9):
            g.release(await g.acquire(1.0), "ok")
        g.release(await g.acquire(1.0), "error")
        for _ in range(9):
            g.release(await g.acquire(1.0), "ok")
        return g.limit

    assert asyncio.run(run()) == 4


async def test_gate_cancel_while_waiting_restores_state():
    g = Gate(initial=1, min_=1, max_=4, increase_after=10)
    e = await g.acquire(1.0)
    waiter = asyncio.create_task(g.acquire(5.0))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    g.release(e, "ok")
    assert g.in_flight == 0 and not g._waiters


async def test_gate_wait_timeout_raises_and_cleans_queue():
    g = Gate(initial=1, min_=1, max_=4, increase_after=10)
    await g.acquire(1.0)
    with pytest.raises(TimeoutError):
        await g.acquire(0.01)
    assert g.in_flight == 1 and not g._waiters


async def test_complete_cancelled_returns_permit():
    gate_open = asyncio.Event()

    async def slow(*a, **kw):
        await gate_open.wait()

    llm = LLM(fake_config(), send=slow)
    task = asyncio.create_task(llm.complete("generator", MSG, deadline_s=5.0))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert llm.gate.in_flight == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert llm.gate.in_flight == 0


async def test_rate_limit_feeds_aimd():
    llm = FakeLLM(fake_config(**{"gate.initial": 4}), script=[RateLimited(0.1), Ok("x")])
    await llm.complete("generator", MSG, deadline_s=5.0)
    assert llm.gate.limit == 2


# --- slot ---


async def test_complete_waits_while_slots_held():
    llm = FakeLLM(fake_config(**{"gate.initial": 2}), script=[Ok("x")])
    async with llm.slot("generator"), llm.slot("generator"):
        task = asyncio.create_task(llm.complete("input_judge", MSG, deadline_s=2.5))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not task.done() and llm.requests == []
    assert (await task).text == "x"


async def test_slot_exception_and_cancel_restore_in_flight():
    llm = FakeLLM()
    with pytest.raises(RuntimeError):
        async with llm.slot("generator"):
            raise RuntimeError("tutor down")
    assert llm.gate.in_flight == 0 and llm.usage["generator"].calls == 1

    async def hold():
        async with llm.slot("generator"):
            await asyncio.sleep(3600)

    t = asyncio.create_task(hold())
    await asyncio.sleep(0)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert llm.gate.in_flight == 0
    u = llm.usage["generator"]
    assert u.calls == 2 and u.retries == 0 and u.tokens_in == 0


async def test_slot_for_faulted_role_skips_gate_and_budget():
    llm = FakeLLM(fake_config(**{"faults.generator": "fake_ok"}), max_calls=0)
    async with llm.slot("generator"):
        assert llm.gate.in_flight == 0
    assert llm.usage["generator"].calls == 0


async def test_gate_wait_is_not_charged_to_deadline():
    clock = FakeClock()
    llm = FakeLLM(fake_config(**{"gate.initial": 2}), script=[Ok("x")], clock=clock)
    async with llm.slot("generator"), llm.slot("generator"):
        task = asyncio.create_task(llm.complete("input_judge", MSG, deadline_s=2.5))
        await asyncio.sleep(0)
        clock.advance(5.0)  # 생성 뒤에 5초 줄을 섬
    res = await task
    assert res.error is None and llm.requests[0]["timeout"] == 2.5


async def test_gate_wait_over_max_wait_is_timeout_without_sending():
    llm = FakeLLM(fake_config(**{"gate.initial": 2, "gate.max_wait_s": 0.01}), script=[Ok("x")])
    async with llm.slot("generator"), llm.slot("generator"):
        res = await llm.complete("input_judge", MSG, deadline_s=2.5)
        assert res.error == "timeout" and llm.requests == []
        assert llm.gate.in_flight == 2
    assert llm.usage["input_judge"].gate_timeouts == 1


async def test_retry_gate_wait_also_not_charged():
    clock = FakeClock()
    llm = FakeLLM(
        fake_config(**{"gate.initial": 1}), script=[RateLimited(0.2), Ok("x")], clock=clock
    )
    res_task = asyncio.create_task(llm.complete("input_judge", MSG, deadline_s=2.5))
    for _ in range(3):
        await asyncio.sleep(0)
    async with llm.slot("generator"):  # 429 뒤 다시 줄을 선다
        clock.advance(10.0)
    res = await res_task
    assert res.error is None and res.attempts == 2


# --- 무작위 스크립트: 예외 0 ---


@settings(max_examples=60, deadline=None)
@given(
    script=st.lists(
        st.sampled_from(
            [Ok("x"), Ok('{"ok": true}'), Malformed(), Timeout(), RateLimited(0.5),
             RateLimited(200), ServerError(), ServerError(401), BadRequest()]
        ),
        min_size=8,
        max_size=8,
    ),
    use_schema=st.booleans(),
    max_calls=st.one_of(st.none(), st.integers(0, 4)),
)  # fmt: skip
def test_random_scripts_never_raise(script, use_schema, max_calls):
    async def run():
        llm = FakeLLM(script=list(script) * 2, max_calls=max_calls)
        return await llm.complete(
            "attack_judge", MSG, schema=Out if use_schema else None, deadline_s=10.0
        )

    res = asyncio.run(run())
    assert res.error in ERRORS
    assert (res.error is None) == (
        res.text is not None and (not use_schema or res.parsed is not None)
    )


# --- Guard가 llm을 갖는다 ---


def test_guard_holds_llm():
    shared = FakeLLM()
    assert Guard(shared.cfg, llm=shared).llm is shared
    assert isinstance(Guard(fake_config()).llm, LLM)


# --- 실제 SDK에 MockTransport: 헤더 파싱과 finish_reason 전달 ---


async def test_openai_send_headers_and_finish_reason():
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx2.Response(429, headers={"retry-after-ms": "250"}, json={"error": {}})
        body = {
            "id": "c1",
            "object": "chat.completion",
            "created": 0,
            "model": "m",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hi"},
                    "finish_reason": "length",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
        return httpx2.Response(200, json=body)

    client = AsyncOpenAI(
        base_url="http://test",
        api_key="test-" + "key",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    clock = FakeClock()
    llm = LLM(fake_config(), send=openai_send(client), clock=clock.now, sleep=clock.sleep)
    res = await llm.complete("generator", MSG, deadline_s=20.0)
    assert (res.text, res.error, res.attempts, res.finish_reason) == ("hi", None, 2, "length")
    assert (res.tokens_in, res.tokens_out) == (3, 2)
    assert clock.slept == [0.25]
    assert len(calls) == 2  # SDK 재시도는 꺼져 있다(max_retries=0)


async def test_openai_send_maps_status_and_connection_errors():
    def handler(request):
        if request.headers.get("x-case") == "boom":
            raise httpx2.ConnectError("down")
        return httpx2.Response(401, json={"error": {"message": "bad key"}})

    client = AsyncOpenAI(
        base_url="http://test",
        api_key="test-" + "key",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    send = openai_send(client)
    with pytest.raises(TransportError) as err:
        await send("m", MSG, response_format=None, temperature=0, max_tokens=8, timeout=1.0)
    assert (err.value.kind, err.value.status) == ("status", 401)
    assert err.value.__cause__ is None and err.value.__suppress_context__
    boom = AsyncOpenAI(
        base_url="http://test",
        api_key="test-" + "key",
        max_retries=0,
        default_headers={"x-case": "boom"},
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    with pytest.raises(TransportError) as err2:
        await openai_send(boom)(
            "m", MSG, response_format=None, temperature=0, max_tokens=8, timeout=1.0
        )
    assert err2.value.kind == "connect"


# --- live (키가 있을 때 로컬에서만) ---


@pytest.mark.live
async def test_live_smoke():
    needed = ("GUARD_LLM_BASE_URL", "GUARD_LLM_API_KEY", "GEN_MODEL")
    if not all(os.environ.get(n) for n in needed):
        pytest.skip("live 환경변수 없음")
    llm = LLM(
        fake_config(
            **{
                "models.generator": os.environ["GEN_MODEL"],
                "models.attack_judge": os.environ["GEN_MODEL"],
            }
        )
    )
    gen = await llm.complete("generator", MSG, deadline_s=20.0)
    js = await llm.complete("attack_judge", MSG, schema=Out, deadline_s=20.0)
    print("생성:", gen.error, gen.finish_reason, "구조화 단:", llm.structured_level, js.error)
    assert gen.error is None
