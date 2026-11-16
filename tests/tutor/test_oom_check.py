"""근거 확인 호출(#201 B, `toggles.oom_check`). LLM은 역할별 가짜다(실제 API를 부르지 않는다)."""

import asyncio

import pytest

from tests.tutor.test_flow import QUESTION, out, state_with
from tutor.answer.templates import TEXT, Fixed
from tutor.judge import flow, oom_check
from tutor.judge.oom_check import OomCheckOut
from tutor.request import parse_request
from tutor.runtime.llm import Result


def _result(parsed=None, error=None, finish="stop"):
    return Result(
        parsed=parsed,
        error=error,
        attempts=1,
        latency_ms=1.0,
        finish_reason=finish,
        completion_tokens=10,
    )


class RoleLLM:
    """역할(main·oom_check)마다 결과를 따로 준다. 부른 역할 순서를 기록한다."""

    def __init__(self, main, check=None, *, check_error=None, finish="stop", boom=False):
        self.main, self.check = main, check
        self.check_error, self.finish, self.boom = check_error, finish, boom
        self.roles: list[str] = []
        self.kwargs: list[dict] = []

    async def complete(self, role, messages, schema, **kw):
        self.roles.append(role)
        self.kwargs.append(kw)
        if role == oom_check.ROLE:
            if self.boom:
                raise RuntimeError("boom")
            return _result(self.check, self.check_error, self.finish)
        return _result(self.main)


def found(value: bool) -> OomCheckOut:
    return OomCheckOut(asked="묻는 것", quote="원문" if value else "", found=value)


ON = {"toggles.oom_check": "after_answer"}


def run(state, text=QUESTION, *, seconds=None):
    async def go():
        now = asyncio.get_running_loop().time()
        deadline = 1e12 if seconds is None else now + seconds
        body = {"messages": [{"role": "user", "content": text}]}
        return await flow.answer_v2(parse_request(body), state, deadline=deadline)

    return asyncio.run(go())


def test_prompt_drops_the_source_comment_line():
    system = oom_check.load_system()
    assert not system.startswith("<!--") and "found" in system


def test_off_makes_no_check_call():
    llm = RoleLLM(out(), found(False))
    resp = run(state_with(llm))
    assert llm.roles == ["main"]
    assert resp["action"] == "answer"


def test_found_false_turns_the_answer_into_fixed_abstain():
    llm = RoleLLM(out(), found(False))
    state = state_with(llm, **ON)
    resp = run(state)
    assert llm.roles == ["main", oom_check.ROLE]
    assert resp == {"answer": TEXT[Fixed.ABSTAIN], "action": "abstain", "citations": []}
    assert llm.kwargs[1]["retries"] == 0


def test_found_true_keeps_the_answer():
    llm = RoleLLM(out(), found(True))
    resp = run(state_with(llm, **ON))
    assert resp["action"] == "answer" and resp["citations"] == ["PY-01-01"]


@pytest.mark.parametrize(
    "main",
    [
        out("answer_seeking", "scaffold"),
        out("out_of_material", "abstain", sentences=[]),
        out("jailbreak_injection", "refuse", sentences=[]),
    ],
)
def test_check_runs_only_for_answers(main):
    llm = RoleLLM(main, found(False))
    run(state_with(llm, **ON))
    assert llm.roles == ["main"]


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"check_error": "timeout"}, "error"),
        ({"finish": "length"}, "truncated"),
        ({"boom": True}, "error"),
    ],
)
def test_check_failure_keeps_the_answer_and_counts_the_reason(kwargs, reason):
    llm = RoleLLM(out(), found(False), **kwargs)
    state = state_with(llm, **ON)
    resp = run(state)
    assert resp["action"] == "answer"
    assert state.oom_skips == {reason: 1}


def test_check_is_skipped_when_little_time_is_left():
    llm = RoleLLM(out(), found(False))
    state = state_with(llm, **ON)
    resp = run(state, seconds=flow.JUDGE_SKIP_S / 2)
    assert llm.roles == ["main"]
    assert resp["action"] == "answer"
    assert state.oom_skips == {"no_time": 1}


def test_check_limit_is_cut_to_the_time_left():
    llm = RoleLLM(out(), found(True))
    run(state_with(llm, **ON), seconds=flow.JUDGE_SKIP_S + 1.0)
    assert llm.kwargs[1]["timeout_s"] <= flow.JUDGE_SKIP_S + 1.0
