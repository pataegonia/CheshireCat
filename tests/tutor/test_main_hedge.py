"""주 호출 겹쳐 보내기(`llm.timeout_s.main_hedge`, flow._complete). LLM은 순서별 가짜다(API 0회).

실제 시간은 0.0x초 단위로 짧게 둔다.
"""

import asyncio

import pytest

from tests.tutor.test_flow import state_with
from tutor.judge import flow
from tutor.runtime.llm import Result

HEDGE = {"llm.timeout_s.main_hedge": 0.05}


def result(error=None):
    return Result(
        parsed=None,
        error=error,
        attempts=1,
        latency_ms=1.0,
        finish_reason="stop",
        completion_tokens=10,
    )


class ScriptLLM:
    """부른 순서대로 (지연, 결과)를 준다. 역할·재시도 수·취소를 기록한다."""

    def __init__(self, *script):
        self.script = list(script)
        self.roles: list[str] = []
        self.retries: list[int] = []
        self.cancelled: list[str] = []

    async def complete(self, role, messages, schema, *, retries, **kw):
        index = len(self.roles)
        self.roles.append(role)
        self.retries.append(retries)
        delay, res = self.script[index]
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self.cancelled.append(role)
            raise
        return res


def run(llm, *, left=1e6, **overrides):
    state = state_with(llm, **{**HEDGE, **overrides})
    complete = flow._complete(state)

    async def go():
        deadline = asyncio.get_running_loop().time() + left
        got = await complete([], None, deadline=deadline)
        await asyncio.sleep(0)  # 취소가 끝나게 한 번 양보
        return got

    return asyncio.run(go())


def test_fast_main_call_sends_no_second_one():
    first = result()
    llm = ScriptLLM((0.0, first))
    assert run(llm) is first
    assert llm.roles == ["main"] and llm.cancelled == []


def test_slow_main_call_sends_a_second_and_the_faster_success_wins():
    first, second = result(), result()
    llm = ScriptLLM((0.5, first), (0.0, second))
    assert run(llm) is second
    assert llm.roles == ["main", flow.MAIN_HEDGE_ROLE]
    assert llm.cancelled == ["main"]  # 느린 첫 호출은 취소한다


def test_first_success_before_the_second_wins_and_cancels_the_second():
    first, second = result(), result()
    llm = ScriptLLM((0.1, first), (0.5, second))
    assert run(llm) is first
    assert llm.roles == ["main", flow.MAIN_HEDGE_ROLE]
    assert llm.cancelled == [flow.MAIN_HEDGE_ROLE]


def test_second_call_has_no_retry_and_main_keeps_its_own():
    llm = ScriptLLM((0.2, result()), (0.0, result()))
    run(llm)
    assert llm.retries == [flow.MAIN_RETRIES, 0]


def test_first_failure_then_second_success_gives_the_success():
    first, second = result("timeout"), result()
    llm = ScriptLLM((0.1, first), (0.2, second))
    assert run(llm) is second
    assert llm.cancelled == []  # 둘 다 끝까지 갔다


def test_both_failing_gives_the_last_failure():
    first, second = result("timeout"), result("invalid_output")
    llm = ScriptLLM((0.1, first), (0.15, second))
    got = run(llm)
    assert got is second and got.error == "invalid_output"


def test_hedge_time_at_or_over_the_main_limit_turns_it_off():
    llm = ScriptLLM((0.1, result()))
    limit = {"llm.timeout_s.main": 0.05}  # main_hedge(0.05) >= main: 느려도 겹쳐 보내지 않는다
    assert run(llm, **limit).error is None and llm.roles == ["main"]


def test_a_failure_before_the_hedge_time_sends_no_second_call():
    first = result("timeout")
    llm = ScriptLLM((0.0, first))
    assert run(llm) is first  # 일찍 실패해도 겹쳐 보내지 않는다(재시도는 TutorLLM 안의 일)
    assert llm.roles == ["main"]


@pytest.mark.parametrize(
    ("left", "second_sent"),
    [(flow.HEDGE_MIN_LEFT_S - 1, False), (flow.HEDGE_MIN_LEFT_S, False), (60.0, True)],
)
def test_no_second_call_when_the_deadline_is_too_close(left, second_sent):
    llm = ScriptLLM((0.15, result()), (0.0, result()))
    run(llm, left=left)
    assert (flow.MAIN_HEDGE_ROLE in llm.roles) is second_sent


def test_outer_cancel_cancels_both_calls_and_is_not_swallowed():
    llm = ScriptLLM((5.0, result()), (5.0, result()))
    complete = flow._complete(state_with(llm, **HEDGE))

    async def go():
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.2):
                await complete([], None, deadline=asyncio.get_running_loop().time() + 1e6)
        await asyncio.sleep(0)
        current = asyncio.current_task()
        return [t for t in asyncio.all_tasks() if t is not current and not t.done()]

    assert asyncio.run(go()) == []
    assert sorted(llm.cancelled) == ["main", flow.MAIN_HEDGE_ROLE]


def test_outer_cancel_before_the_hedge_time_cancels_the_first_call():
    llm = ScriptLLM((5.0, result()))
    complete = flow._complete(state_with(llm, **{"llm.timeout_s.main_hedge": 10}))

    async def go():
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await complete([], None, deadline=asyncio.get_running_loop().time() + 1e6)
        await asyncio.sleep(0)

    asyncio.run(go())
    assert llm.cancelled == ["main"] and llm.roles == ["main"]
