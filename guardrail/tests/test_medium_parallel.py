"""실험: 출력 판정을 low·medium 두 호출로 함께 보낸다(output.medium_parallel).

medium이 맞는 답을 내면 medium, 아니면 low, 둘 다 실패면 실패 기본값. 가짜 전송으로 0ms에 돈다.
"""

import json
import time

import pytest
from fakes import Hang, HttpStatus, Malformed, Ok, Timeout
from test_guard import GROUNDED, PING, UNGROUNDED, _role, calls, make, req

from guardrail import guard as guard_module
from guardrail.schema import OutputVerdict

pytestmark = pytest.mark.anyio
LOW_OK = Ok(json.dumps(GROUNDED))  # low는 근거 있음
MEDIUM_OK = Ok(json.dumps(UNGROUNDED, ensure_ascii=False))  # medium은 근거 없음 + 인용


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def no_rule(monkeypatch):
    monkeypatch.setattr(guard_module, "literal_rule", lambda *a, **k: None)


def _is_medium(kw) -> bool:
    return kw.get("reasoning_effort") == "medium"


def pair(low=LOW_OK, medium=MEDIUM_OK):
    def respond(kw):
        if kw["messages"] == PING:
            return Ok()
        if _role(kw) == "input":
            return Ok(json.dumps({"evidence": "e", "category": "normal"}))
        return medium if _is_medium(kw) else low

    return respond


def _sent(fake):
    out = calls(fake, "output")
    return [r for r in out if not _is_medium(r)], [r for r in out if _is_medium(r)]


async def test_medium_answer_is_used_when_it_succeeds(no_rule):
    guard, fake = await make(pair(), output__medium_parallel=True)
    j = await guard.judge(req())
    assert (j.output_source, j.nano_grounded, j.errors) == ("med", False, ())
    assert j.response.output.grounded is False and j.calls == 3  # 입력 + low + medium
    low, medium = _sent(fake)
    # 가짜 medium은 바로 끝나서 low는 나가기 전에 취소될 수 있다(실제로는 low가 먼저 끝난다)
    assert len(medium) == 1 and len(low) <= 1
    # medium 호출만 호출별 값(llm.per_role.output_judge_medium)으로 나간다
    assert medium[0]["max_completion_tokens"] == 2000 and medium[0]["timeout"].read == 16
    assert guard.llm.calls["output_judge_medium"] == 1


@pytest.mark.parametrize("failure", [Timeout(), Malformed(), HttpStatus(500)])
async def test_low_answer_is_used_when_medium_fails(no_rule, failure):
    guard, fake = await make(pair(medium=failure), output__medium_parallel=True)
    j = await guard.judge(req())
    assert (j.output_source, j.nano_grounded, j.errors) == ("low", True, ())
    assert j.response.output == OutputVerdict(True, (), True) and j.calls == 3
    low, medium = _sent(fake)
    assert (len(low), len(medium)) == (1, 1)  # medium은 재시도하지 않는다
    # low 호출은 기존 값 그대로다. 프롬프트는 둘이 같다
    assert low[0]["reasoning_effort"] == "low" and "timeout" not in low[0]
    assert low[0]["max_completion_tokens"] == 1600 and medium[0]["messages"] == low[0]["messages"]


@pytest.mark.parametrize(("default", "grounded"), [("grounded", True), ("ungrounded", False)])
async def test_both_failing_uses_failure_default(no_rule, default, grounded):
    guard, fake = await make(
        pair(low=Malformed(), medium=Timeout()),
        output__medium_parallel=True,
        output__failure_default=default,
    )
    j = await guard.judge(req())
    assert j.response.output == OutputVerdict(grounded, (), True) and j.output_tier == 4
    assert (j.output_source, j.nano_grounded, j.errors) == ("default", None, ("output_judge",))
    low, medium = _sent(fake)
    assert (len(low), len(medium)) == (2, 1)  # low는 기존대로 재시도 1회


async def test_medium_is_cut_before_the_deadline_and_low_is_kept(no_rule):
    # medium이 끝나지 않으면 마감 0.5초 전(MEDIUM_MARGIN_S)에 끊고 이미 받은 low 답을 쓴다
    guard, _ = await make(pair(medium=Hang()), output__medium_parallel=True, budget_s=0.7)
    t0 = time.monotonic()
    j = await guard.judge(req())
    assert time.monotonic() - t0 < guard.cfg.budget_s
    assert (j.output_source, j.nano_grounded, j.errors) == ("low", True, ())


async def test_toggle_off_sends_one_output_call(no_rule):
    guard, fake = await make(pair())  # make는 medium_parallel을 끈다
    j = await guard.judge(req())
    low, medium = _sent(fake)
    assert (len(low), len(medium)) == (1, 0)
    assert (j.output_source, j.nano_grounded, j.calls) == (None, True, 2)


async def test_no_candidate_sends_no_medium_call(no_rule):
    guard, fake = await make(pair(), output__medium_parallel=True)
    j = await guard.judge(req(answer=None, has=False))
    assert calls(fake, "output") == [] and (j.output_source, j.calls) == (None, 1)


async def test_tutor_shared_judge_output_sends_no_medium_call(no_rule):
    # 튜터 공유 API는 그대로다(호출 1회, 재시도 없음)
    guard, fake = await make(pair(), output__medium_parallel=True)
    await guard.judge_output([{"role": "user", "content": "q"}], "답변", "normal")
    low, medium = _sent(fake)
    assert (len(low), len(medium)) == (1, 0)


async def test_toggle_needs_the_per_role_entry():
    with pytest.raises(ValueError, match="output_judge_medium"):
        await make(output__medium_parallel=True, llm__per_role={})
