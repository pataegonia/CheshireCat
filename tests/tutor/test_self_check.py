"""2차 자체 검증(`toggles.self_check`). LLM은 역할별 가짜다(실제 API를 부르지 않는다)."""

import asyncio
import types

import pytest
from pydantic import ValidationError

from tests.tutor.test_flow import QUESTION, out, state_with
from tutor.judge import flow, self_check
from tutor.judge.main_call import EvidenceOut, SentenceOut
from tutor.judge.self_check import SelfCheckOut
from tutor.request import parse_request
from tutor.runtime import config
from tutor.runtime.llm import Result

VAGUE = [SentenceOut(text="야구 점수판은 점수를 기록하는 예제예요.", kind="non_claim", evidence=[])]
FIXED = [
    SentenceOut(
        text="야구 점수판 예제는 3회까지의 점수를 기록해요.",
        kind="claim",
        evidence=[EvidenceOut(sid="PY-01-01", quote="3회까지의 점수를 기록해요")],
    )
]


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
    """역할(main·self_check)마다 결과를 따로 준다. 부른 역할 순서와 메시지를 기록한다."""

    def __init__(self, main, check=None, *, check_error=None, finish="stop", boom=False):
        self.main, self.check = main, check
        self.check_error, self.finish, self.boom = check_error, finish, boom
        self.roles: list[str] = []
        self.calls: list[dict] = []

    async def complete(self, role, messages, schema, **kw):
        self.roles.append(role)
        self.calls.append({"messages": messages, "schema": schema, **kw})
        if role == self_check.ROLE:
            if self.boom:
                raise RuntimeError("boom")
            return _result(self.check, self.check_error, self.finish)
        return _result(self.main)


def verdict(covered, sentences=(), quote="3회까지의 점수를 기록해요"):
    return SelfCheckOut(asked="점수판이 기록하는 범위", quote=quote, covered=covered,
                        sentences=list(sentences))  # fmt: skip


def run(state, text=QUESTION, *, seconds=None, probe=None):
    async def go():
        now = asyncio.get_running_loop().time()
        deadline = 1e12 if seconds is None else now + seconds
        body = {"messages": [{"role": "user", "content": text}]}
        return await flow.answer_v2(parse_request(body), state, deadline=deadline)

    token = flow.ROW_PROBE.set(probe)
    try:
        return asyncio.run(go())
    finally:
        flow.ROW_PROBE.reset(token)


ON = {"toggles.self_check": "bundle"}


def test_prompt_drops_the_source_comment_line():
    system = self_check.load_system()
    assert not system.startswith("<!--") and "covered" in system


def test_off_makes_no_check_call():
    llm = RoleLLM(out(sentences=VAGUE), verdict(False, FIXED))
    resp = run(state_with(llm))
    assert llm.roles == ["main"]
    assert resp["answer"] == VAGUE[0].text


def test_missing_value_is_rewritten_and_cited():
    llm = RoleLLM(out(sentences=VAGUE), verdict(False, FIXED))
    state = state_with(llm, **ON)
    probe: dict = {}
    resp = run(state, probe=probe)
    assert llm.roles == ["main", self_check.ROLE]
    assert resp["action"] == "answer"
    assert resp["answer"] == FIXED[0].text
    assert resp["citations"][0] == "PY-01-01"
    assert state.self_check_counts == {"rewrote": 1}
    assert probe["self_check"] == "rewrote"
    call = llm.calls[1]
    assert call["retries"] == 0 and call["schema"] is SelfCheckOut
    user = call["messages"][1]["content"]
    assert "<초안>\n- 야구 점수판은 점수를 기록하는 예제예요.\n</초안>" in user
    assert "<근거 묶음>" in user and '판단대상="예"' in user


@pytest.mark.parametrize(
    "check",
    [verdict(True, FIXED), verdict(False, FIXED, quote=" "), verdict(False, [])],
    ids=["covered", "no_quote", "no_sentences"],
)
def test_draft_is_kept_when_nothing_to_fix(check):
    llm = RoleLLM(out(sentences=VAGUE), check)
    state = state_with(llm, **ON)
    resp = run(state)
    assert resp["answer"] == VAGUE[0].text
    assert state.self_check_counts == {"kept": 1}


def test_rewrite_citing_a_section_outside_the_shown_ones_is_dropped():
    elsewhere = [
        SentenceOut(
            text="다른 섹션 문장이에요.",
            kind="claim",
            evidence=[EvidenceOut(sid="ZZ-99-99", quote="없는 문구")],
        )
    ]
    llm = RoleLLM(out(sentences=VAGUE), verdict(False, elsewhere))
    state = state_with(llm, **{"toggles.self_check": "cited"})
    resp = run(state)
    assert resp["answer"] == VAGUE[0].text
    assert state.self_check_counts == {"no_evidence": 1}


@pytest.mark.parametrize(
    "main",
    [
        out("pii_disclosure", "answer"),
        out("answer_seeking", "scaffold"),
        out("out_of_material", "abstain", sentences=[]),
        out("jailbreak_injection", "refuse", sentences=[]),
    ],
)
def test_check_runs_only_for_normal_answers(main):
    llm = RoleLLM(main, verdict(False, FIXED))
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
def test_check_failure_keeps_the_draft_and_counts_the_reason(kwargs, reason):
    llm = RoleLLM(out(sentences=VAGUE), verdict(False, FIXED), **kwargs)
    state = state_with(llm, **ON)
    resp = run(state)
    assert resp["answer"] == VAGUE[0].text
    assert state.self_check_counts == {reason: 1}


def test_check_is_skipped_when_little_time_is_left():
    llm = RoleLLM(out(sentences=VAGUE), verdict(False, FIXED))
    state = state_with(llm, **ON)
    resp = run(state, seconds=flow.JUDGE_SKIP_S / 2)
    assert llm.roles == ["main"]
    assert resp["answer"] == VAGUE[0].text
    assert state.self_check_counts == {"no_time": 1}


def test_check_limit_is_cut_to_the_time_left():
    llm = RoleLLM(out(sentences=VAGUE), verdict(True))
    run(state_with(llm, **ON), seconds=flow.JUDGE_SKIP_S + 1.0)
    assert llm.calls[1]["timeout_s"] <= flow.JUDGE_SKIP_S + 1.0


def _sec(sid):
    return types.SimpleNamespace(id=sid, text=sid, tables=())


def test_cited_scope_keeps_cited_sections_and_top1_in_search_order():
    sections = [_sec("A"), _sec("B"), _sec("C"), _sec("D")]
    draft = [SentenceOut(text="t", kind="claim", evidence=[EvidenceOut(sid="C", quote="q")])]
    got = self_check.scoped_sections(sections, draft, "cited")
    assert [s.id for s in got] == ["A", "C"]
    assert self_check.scoped_sections(sections, draft, "bundle") == sections
    assert self_check.scoped_sections([], draft, "cited") == []


@pytest.mark.parametrize(
    "extra",
    [
        {"toggles.intent_override": "serial"},  # 주 호출 2 + T 1 + 검증 1 = 4
        {"toggles.oom_check": "after_answer"},
        {"toggles.guard_classify": "parallel"},
    ],
)
def test_self_check_fits_the_call_limit_with_one_extra_call(extra):
    cfg = config.load_config(overrides={**ON, **extra})
    assert cfg.toggles.self_check == "bundle"


@pytest.mark.parametrize(
    "extra",
    [
        {"toggles.intent_override": "parallel", "toggles.guard_classify": "parallel"},
        {"toggles.intent_override": "serial", "toggles.oom_check": "after_answer"},
        {"toggles.verify": "guard", "toggles.guard_classify": "parallel"},
    ],
)
def test_self_check_over_the_call_limit_fails_at_load(extra):
    with pytest.raises(ValidationError, match="TU-13"):
        config.load_config(overrides={**ON, **extra})


def test_self_check_rejects_unknown_scope():
    with pytest.raises(ValidationError):
        config.load_config(overrides={"toggles.self_check": "on"})
