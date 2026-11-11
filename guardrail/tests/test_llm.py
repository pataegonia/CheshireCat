"""S1.3 LLM 게이트: 재시도·마감·probe·게이트·오류 코드 (master-plan §4.3, GR-10).

FakeLLM + FakeClock으로 0ms에 돈다. 실제 API는 `-m live`에서만.
"""

import asyncio
import email.utils
import os
import time
from collections import Counter

import pytest
from fakes import (
    BadRequest,
    Boom,
    ConnectFail,
    FakeClock,
    FakeLLM,
    Hang,
    HttpStatus,
    Malformed,
    Ok,
    RateLimited,
    ServerError,
    Timeout,
)

from guardrail.config import load_config
from guardrail.judges.schemas import InputOut, strict_schema
from guardrail.llm import LLM

pytestmark = pytest.mark.anyio

BASE = load_config().llm
MSGS = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USER"}]
OK_IN = '{"evidence": "e", "category": "normal"}'


@pytest.fixture(autouse=True)
def _model_env(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "test-model")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def make(*steps, respond=None, **over):
    clock = FakeClock()
    fake = FakeLLM(*steps, respond=respond)
    llm = LLM(BASE.model_copy(update=over), send=fake.send, clock=clock, sleep=clock.sleep)
    return llm, fake, clock


async def call(llm, clock, budget=20.0):
    return await llm.complete("input_judge", MSGS, InputOut, deadline=clock() + budget)


# --- 정상·요청 인자 ---


async def test_ok_carries_finish_reason_and_tokens():
    llm, _, clock = make(Ok(OK_IN, "stop", 7))
    r = await call(llm, clock)
    assert r.parsed == InputOut(evidence="e", category="normal")
    assert (r.error, r.attempts, r.finish_reason, r.completion_tokens) == (None, 1, "stop", 7)
    assert llm.calls == Counter({"input_judge": 1})


async def test_length_finish_and_missing_usage():
    llm, _, clock = make(Ok(OK_IN, "length", None))
    r = await call(llm, clock)
    assert (r.finish_reason, r.completion_tokens) == ("length", None)


async def test_request_args_are_fixed():
    llm, fake, clock = make(Ok(OK_IN))
    await call(llm, clock)
    kw = fake.requests[0]
    assert set(kw) == {
        "model",
        "messages",
        "reasoning_effort",
        "verbosity",
        "max_completion_tokens",
        "response_format",
    }
    assert not {"temperature", "max_tokens", "logprobs"} & set(kw)
    assert kw["model"] == "test-model"
    assert kw["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "InputOut", "schema": strict_schema(InputOut), "strict": True},
    }


async def test_null_params_are_not_sent():
    llm, fake, clock = make(
        Ok(OK_IN), reasoning_effort=None, verbosity=None, max_completion_tokens=None
    )
    await call(llm, clock)
    assert set(fake.requests[0]) == {"model", "messages", "response_format"}


async def test_json_object_mode_appends_schema_to_system():
    llm, fake, clock = make(Ok(OK_IN), Ok(OK_IN), structured_output="json_object")
    await call(llm, clock)
    kw = fake.requests[0]
    system = kw["messages"][0]["content"]
    assert kw["response_format"] == {"type": "json_object"}
    assert system.startswith("SYS") and '"evidence"' in system
    assert MSGS[0]["content"] == "SYS"  # 호출자의 목록을 바꾸지 않는다
    await llm.complete("input_judge", [{"role": "user", "content": "U"}], InputOut, deadline=99)
    assert fake.requests[1]["messages"][0]["role"] == "system"


# --- probe·start ---


def reject(param):
    return lambda kw: BadRequest() if param in kw else Ok(OK_IN)


async def test_probe_turns_off_only_the_rejected_param():
    llm, fake, _ = make(respond=reject("verbosity"))
    result = await llm.probe()
    assert result == {
        "base": None,
        "reasoning_effort": None,
        "verbosity": "bad_request",
        "max_completion_tokens": None,
        "response_format": None,
    }
    assert llm.params["verbosity"] is None
    assert llm.params["reasoning_effort"] == "low" and llm.params["max_completion_tokens"] == 800
    assert llm.structured == "json_schema"
    # 파라미터마다 따로 시험한다
    extras = [set(kw) - {"model", "messages"} for kw in fake.requests]
    assert sorted(map(sorted, extras)) == [
        [],  # 맨 요청
        ["max_completion_tokens"],
        ["reasoning_effort"],
        ["response_format"],
        ["verbosity"],
    ]


async def test_probe_json_schema_rejection_falls_back_to_json_object():
    llm, _, _ = make(respond=reject("response_format"))
    await llm.probe()
    assert llm.structured == "json_object"
    assert llm.params["verbosity"] == "low"


async def test_probe_keeps_param_on_non_rejection_error():
    llm, _, _ = make(respond=lambda kw: ServerError())
    result = await llm.probe()
    assert result == {"base": "server"}
    assert llm.params["verbosity"] == "low" and llm.structured == "json_schema"


async def test_probe_skips_response_format_when_json_object_configured():
    llm, fake, _ = make(respond=lambda kw: Ok(OK_IN), structured_output="json_object")
    result = await llm.probe()
    assert "response_format" not in result and len(fake.requests) == 4


async def test_start_twice_probes_once():
    llm, fake, _ = make(respond=lambda kw: Ok(OK_IN))
    await llm.start()
    await llm.start()
    assert len(fake.requests) == 5


async def test_start_without_env_is_a_config_error(monkeypatch):
    for name in ("OPENAI_API_KEY", "OPENAI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        await LLM(BASE).start()


async def test_complete_before_start_is_a_programming_error():
    with pytest.raises(RuntimeError):
        await call(LLM(BASE), FakeClock())


# --- 재시도·마감 ---


async def test_429_waits_retry_after_then_succeeds():
    llm, _, clock = make(RateLimited("2"), Ok(OK_IN))
    r = await call(llm, clock)
    assert r.parsed and r.attempts == 2 and clock.slept == [2.0]


async def test_retry_after_ms_wins_over_retry_after():
    llm, _, clock = make(RateLimited("1500", "retry-after-ms"), Ok(OK_IN))
    await call(llm, clock)
    assert clock.slept == [1.5]


async def test_retry_after_http_date():
    when = email.utils.formatdate(time.time() + 5, usegmt=True)
    llm, _, clock = make(RateLimited(when), Ok(OK_IN))
    await call(llm, clock)
    assert 3.0 < clock.slept[0] <= 5.0


async def test_unparseable_retry_after_falls_back_to_jitter():
    llm, _, clock = make(RateLimited("soon"), RateLimited("nan"), Ok(OK_IN), retries=2)
    await call(llm, clock)
    lo, hi = BASE.retry_jitter_s
    assert len(clock.slept) == 2 and all(lo <= s <= hi for s in clock.slept)


async def test_retry_after_past_deadline_fails_fast():
    llm, fake, clock = make(RateLimited("30"), Ok(OK_IN))
    r = await call(llm, clock, budget=20)
    assert (r.error, r.attempts, clock.slept, len(fake.requests)) == ("rate_limit", 1, [], 1)


async def test_retry_after_over_sdk_cap_fails_fast():
    llm, _, clock = make(RateLimited("121"), Ok(OK_IN))
    assert (await call(llm, clock, budget=999)).error == "rate_limit"


async def test_timeout_retries_then_fails():
    llm, _, clock = make(Timeout(), Timeout())
    r = await call(llm, clock)
    lo, hi = BASE.retry_jitter_s
    assert (r.error, r.attempts, r.parsed) == ("timeout", 2, None)
    assert r.finish_reason is None and r.completion_tokens is None
    assert len(clock.slept) == 1 and lo <= clock.slept[0] <= hi


async def test_no_retry_when_wait_plus_read_exceeds_deadline():
    llm, fake, clock = make(Timeout(), Ok(OK_IN))
    r = await call(llm, clock, budget=BASE.timeout_s.read)  # 대기 + read > 마감
    assert (r.error, r.attempts, len(fake.requests)) == ("timeout", 1, 1)


async def test_retries_zero_means_single_attempt():
    llm, _, clock = make(Timeout(), Ok(OK_IN), retries=0)
    assert (await call(llm, clock)).attempts == 1


@pytest.mark.parametrize(
    ("step", "code", "attempts"),
    [
        (HttpStatus(408), "timeout", 2),
        (HttpStatus(409), "server", 2),
        (HttpStatus(503), "server", 2),
        (ConnectFail(), "server", 2),
        (HttpStatus(401), "auth", 1),
        (HttpStatus(403), "auth", 1),
        (BadRequest(), "bad_request", 1),
        (HttpStatus(422), "bad_request", 1),
        (HttpStatus(404), "bad_request", 1),
    ],
)
async def test_error_classification(step, code, attempts):
    llm, _, clock = make(step, step)
    r = await call(llm, clock)
    assert (r.error, r.attempts) == (code, attempts)


async def test_x_should_retry_header_overrides():
    llm, _, clock = make(HttpStatus(500, {"x-should-retry": "false"}), Ok(OK_IN))
    assert (await call(llm, clock)).attempts == 1
    llm, _, clock = make(HttpStatus(400, {"x-should-retry": "true"}), Ok(OK_IN))
    assert (await call(llm, clock)).attempts == 2


async def test_malformed_json_reasked_once():
    llm, fake, clock = make(Malformed(), Ok(OK_IN))
    r = await call(llm, clock)
    assert r.parsed and r.attempts == 2 and len(fake.requests) == 2


@pytest.mark.parametrize(
    "content", ["not json", '{"evidence": "e"}', '{"evidence":"e","category":"normal","x":1}']
)
async def test_schema_mismatch_twice_is_bad_json(content):
    llm, _, clock = make(Ok(content, "stop", 3), Ok(content, "length", 4))
    r = await call(llm, clock)
    assert (r.error, r.attempts, r.parsed) == ("bad_json", 2, None)
    assert (r.finish_reason, r.completion_tokens) == ("length", 4)


async def test_response_without_choices_is_bad_json():
    async def empty(kwargs):
        return object()

    clock = FakeClock()
    llm = LLM(BASE.model_copy(update={"retries": 0}), send=empty, clock=clock, sleep=clock.sleep)
    r = await call(llm, clock)
    assert (r.error, r.finish_reason, r.completion_tokens) == ("bad_json", None, None)


# --- 예산·게이트·취소 ---


async def test_budget_exceeded_makes_no_call():
    llm, fake, clock = make(Ok(OK_IN))
    llm.max_calls = 1
    assert (await call(llm, clock)).parsed
    r = await call(llm, clock)
    assert (r.error, r.attempts, len(fake.requests)) == ("budget", 0, 1)


async def test_budget_blocks_the_retry_too():
    llm, fake, clock = make(Timeout(), Ok(OK_IN))
    llm.max_calls = 1
    r = await call(llm, clock)
    assert (r.error, r.attempts, len(fake.requests)) == ("budget", 1, 1)


async def test_saturated_gate_past_deadline_is_timeout():
    llm, fake, clock = make(Ok(OK_IN), gate=1)
    async with llm.slot():  # slot()이 같은 세마포어
        r = await call(llm, clock, budget=0.01)
        assert (r.error, r.attempts, len(fake.requests)) == ("timeout", 0, 0)
        assert (await call(llm, clock, budget=0)).error == "timeout"
    assert (await call(llm, clock)).parsed  # 허가가 돌아왔다


async def test_send_past_deadline_is_timeout():
    llm, _, clock = make(Hang())
    r = await call(llm, clock, budget=0.01)
    assert (r.error, r.attempts) == ("timeout", 1)


async def test_cancel_propagates_and_returns_permit():
    llm, fake, clock = make(Hang(), gate=1)
    task = asyncio.create_task(call(llm, clock))
    async with asyncio.timeout(1):
        while not fake.requests:
            await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with asyncio.timeout(1), llm.slot():
        pass


# --- 조용한 실패 방지 ---


async def test_errors_carry_codes_not_messages():
    secret = "SECRET-LEARNER-TEXT"  # noqa: S105
    llm, _, clock = make(BadRequest(secret))
    r = await call(llm, clock)
    assert r.error == "bad_request" and secret not in repr(r)
    llm, _, clock = make(Boom(secret))
    r = await call(llm, clock)
    assert r.error == "server" and r.attempts == 1 and secret not in repr(r)
    assert llm.unexpected == Counter({"RuntimeError": 1})  # 클래스 이름만


# --- live ---


@pytest.mark.live
async def test_live_probe_and_input_call():
    if not (os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_MODEL")):
        pytest.skip("OPENAI_API_KEY·OPENAI_MODEL 없음")
    llm = LLM(BASE)
    await llm.start()
    msgs = [
        {"role": "system", "content": "학습자 발화를 분류하라. 근거 한 문장 뒤에 범주."},
        {"role": "user", "content": "<target>안녕하세요</target>"},
    ]
    r = await llm.complete("input_judge", msgs, InputOut, deadline=time.monotonic() + 20)
    assert r.error is None and r.parsed is not None


async def test_probe_base_failure_disables_nothing():
    llm, _, _ = make(respond=lambda kw: BadRequest())
    assert await llm.probe() == {"base": "bad_request"}
    assert llm.params["verbosity"] == "low" and llm.structured == "json_schema"


async def test_start_fails_on_rejected_credentials():
    llm, _, _ = make(respond=lambda kw: HttpStatus(401))
    with pytest.raises(ValueError, match="인증"):
        await llm.start()
    assert llm.probe_result == {"base": "auth"}


async def test_absurd_retry_after_date_does_not_raise():
    llm, _, clock = make(RateLimited("Mon, 01 Jan 99999 00:00:00 GMT"), Ok(OK_IN))
    assert (await call(llm, clock)).parsed
    assert 0.2 <= clock.slept[0] <= 0.5


async def test_bad_retry_after_ms_falls_through_to_retry_after():
    step = HttpStatus(429, {"retry-after-ms": "x", "retry-after": "3"})
    llm, _, clock = make(step, Ok(OK_IN))
    await call(llm, clock)
    assert clock.slept == [3.0]


async def test_budget_holds_under_concurrency():
    llm, fake, clock = make(Ok(OK_IN), Ok(OK_IN), Ok(OK_IN), gate=1)
    llm.max_calls = 1
    rs = await asyncio.gather(*(call(llm, clock) for _ in range(3)))
    assert sorted(r.error or "" for r in rs) == ["", "budget", "budget"]
    assert len(fake.requests) == 1


async def test_retry_dropped_when_wait_overran_the_margin():
    clock = FakeClock()
    fake = FakeLLM(Timeout(), Ok(OK_IN))

    async def slow_sleep(seconds):  # 대기 중에 게이트 줄·정지로 시간이 더 갔다
        clock.now += seconds + 15

    llm = LLM(BASE, send=fake.send, clock=clock, sleep=slow_sleep)
    r = await call(llm, clock, budget=20)
    assert r.error == "timeout" and r.attempts == 1 and len(fake.requests) == 1


async def test_result_repr_hides_parsed_text():
    llm, _, clock = make(Ok('{"evidence": "SECRET-TEXT", "category": "normal"}'))
    assert "SECRET-TEXT" not in repr(await call(llm, clock))
