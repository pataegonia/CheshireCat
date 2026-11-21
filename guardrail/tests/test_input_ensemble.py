"""실험: 입력 1차를 3표로 낸다(input.ensemble, 기본 끔).

A = 지금 1차(범주, kNN 예시), B = 요청 유형(input_judge_act.md, 예시 없음), C = A에서 예시만 뺌.
합치는 규칙은 `decide.ensemble_vote`, 흐름은 가짜 전송으로 0ms에 본다.
"""

import json

import pytest
from fakes import Malformed, Ok
from test_guard import PING, calls, make, req

from guardrail.config import load_config
from guardrail.decide import ensemble_vote
from guardrail.eval.run import calls_per_row, estimate_calls, parse_sets

pytestmark = pytest.mark.anyio
BLOCK = frozenset(load_config().block_categories)
TO_ACT = {
    "normal": "learning_question",
    "answer_seeking": "only_final_answer",
    "jailbreak_injection": "override_tutor_role",
    "off_topic": "make_offcourse_output",
    "harmful_content": "insult_or_hate",
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def no_rule(monkeypatch):
    from guardrail import guard as guard_module

    monkeypatch.setattr(guard_module, "literal_rule", lambda *a, **k: None)


# --- 합치는 규칙 ---


@pytest.mark.parametrize(
    ("votes", "want"),
    [
        (["normal", None, None], "normal"),  # A만 유효 = 지금 그대로
        (["off_topic", None, None], "off_topic"),
        ([None, None, None], None),
        (["normal", "off_topic", "off_topic"], "off_topic"),  # 다수결 차단
        (
            ["off_topic", "normal", "answer_seeking"],
            "normal",
        ),  # 다수결 허용, A가 차단이면 허용 중 앞
        (["off_topic", "answer_seeking", "normal"], "answer_seeking"),
        (["off_topic", "normal", "normal"], "normal"),
        (["answer_seeking", "normal", "normal"], "answer_seeking"),  # 허용이면 A가 허용일 때 A
        (["normal", "off_topic", None], "normal"),  # 유효 2표가 갈리면 A 쪽
        (["off_topic", "normal", None], "off_topic"),
        ([None, "off_topic", "normal"], "off_topic"),  # A 무효, B·C가 갈리면 B 쪽
        ([None, "normal", "harmful_content"], "normal"),
        ([None, "harmful_content", "harmful_content"], "harmful_content"),
        # 차단 범주는 가장 많은 것, 동수면 A, A가 차단이 아니면 B
        (["off_topic", "harmful_content", "harmful_content"], "harmful_content"),
        (["off_topic", "harmful_content", "normal"], "off_topic"),
        (["normal", "harmful_content", "off_topic"], "harmful_content"),
        (["jailbreak_injection", "off_topic", "harmful_content"], "jailbreak_injection"),
    ],
)
def test_ensemble_vote(votes, want):
    assert ensemble_vote(votes, BLOCK) == want


# --- 흐름 ---


def _voter(kw) -> str:
    if "InputAct" in kw["response_format"]["json_schema"]["name"]:
        return "B"
    return "A" if "<examples>" in kw["messages"][1]["content"] else "C"


def voters(a, b, c, jb="apply"):
    """투표자별 응답. 값이 str이면 그 범주(B는 요청 유형으로), 아니면 그 단계(Malformed 등)."""

    def respond(kw):
        if kw["messages"] == PING:
            return Ok()
        name = kw["response_format"]["json_schema"]["name"]
        if name.startswith("JbCheck"):
            return Ok(json.dumps({"evidence": "e", "verdict": jb}))
        if not name.startswith("Input"):
            return Ok(json.dumps({"answer_quote": "", "kb_quote": "", "grounded": True,
                                  "refuses": False}))  # fmt: skip
        step = {"A": a, "B": b, "C": c}[_voter(kw)]
        if not isinstance(step, str):
            return step
        if _voter(kw) == "B":
            return Ok(json.dumps({"request": "r", "act": TO_ACT[step]}))
        return Ok(json.dumps({"evidence": "e", "category": step}))

    return respond


async def test_off_by_default_sends_one_input_call(no_rule):
    guard, fake = await make(voters("normal", "off_topic", "off_topic"))
    j = await guard.judge(req())
    assert j.response.category == "normal" and j.input_votes is None
    assert len(calls(fake, "input")) == 1 and j.calls == 2


async def test_majority_block_overrides_a(no_rule):
    guard, fake = await make(voters("normal", "off_topic", "off_topic"), input__ensemble=True)
    j = await guard.judge(req())
    assert (
        j.response.category == "off_topic" and j.response.output.safe is False
    )  # GR-08: 차단 + 거절 아님
    assert j.output_tier == 1  # 합친 범주(차단)로 출력 단 ①을 계산한다
    assert sorted(_voter(r) for r in calls(fake, "input")) == ["A", "B", "C"]
    assert (j.calls, j.errors, j.input_source) == (4, (), "nano")  # 3표 + 출력
    assert j.input_votes == "normal,off_topic,off_topic"
    b = next(r for r in calls(fake, "input") if _voter(r) == "B")
    assert "요청 유형" in b["messages"][0]["content"]  # input_judge_act.md


async def test_a_failing_uses_b_and_c(no_rule):
    guard, _ = await make(voters(Malformed(), "answer_seeking", "normal"), input__ensemble=True)
    j = await guard.judge(req())
    assert (j.response.category, j.input_source, j.errors) == ("answer_seeking", "nano", ())
    assert j.input_votes == "-,answer_seeking,normal"


async def test_all_failing_takes_todays_failure_path(no_rule):
    guard, _ = await make(voters(Malformed(), Malformed(), Malformed()), input__ensemble=True)
    j = await guard.judge(req())
    assert j.input_source in ("fallback", "default") and "input_judge" in j.errors


async def test_jb_check_runs_once_on_the_combined_result(no_rule):
    guard, fake = await make(
        voters("normal", "jailbreak_injection", "jailbreak_injection", jb="study"),
        input__ensemble=True,
    )
    j = await guard.judge(req())
    assert j.response.category == "normal"  # 합친 탈옥을 2차가 풀었다
    assert len(calls(fake, "jb_check")) == 1 and j.calls == 5


async def test_classify_stays_single_call():
    guard, fake = await make(voters("normal", "off_topic", "off_topic"), input__ensemble=True)
    assert await guard.classify([{"role": "user", "content": "질문"}]) == "normal"
    assert len(calls(fake, "input")) == 1


def test_call_estimates_count_the_two_extra_voters():
    off = load_config()
    on = load_config(overrides=parse_sets(["input.ensemble=true"]))
    assert on.input.ensemble is True
    assert calls_per_row(on, True) == calls_per_row(off, True) + 2
    rows = [object()] * 3
    est = estimate_calls({"x": on}, rows, 1, on.llm, input_only=True)
    assert est - estimate_calls({"x": off}, rows, 1, off.llm, input_only=True) == 6
