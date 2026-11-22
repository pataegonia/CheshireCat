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


async def test_raising_voter_is_invalid_and_a_raising_alone_keeps_todays_error(
    no_rule, monkeypatch
):
    from guardrail.judges import input_judge

    real = input_judge._ask

    async def b_c_raise(window, target, examples, llm, cfg, *rest):
        if cfg.input.label_mode == "act" or not examples:  # B·C
            raise RuntimeError("voter")
        return await real(window, target, examples, llm, cfg, *rest)

    monkeypatch.setattr(input_judge, "_ask", b_c_raise)
    guard, _ = await make(voters("off_topic", "normal", "normal"), input__ensemble=True)
    j = await guard.judge(req())
    assert (j.response.category, j.errors, j.input_votes) == ("off_topic", (), "off_topic,-,-")

    async def all_raise(*a):
        raise RuntimeError("all")

    monkeypatch.setattr(input_judge, "_ask", all_raise)
    j = await guard.judge(req())
    assert "input_judge:RuntimeError" in j.errors and j.input_source in ("fallback", "default")


async def test_input_only_harness_records_votes():
    from fakes import FakeLLM
    from test_guard import _public

    from guardrail.data import labeled_rows
    from guardrail.eval.run import InputOnlyGuard
    from guardrail.llm import LLM

    overrides = {
        "input.ensemble": True,
        "toggles.request_check": False,  # --input-only가 받지 않는 토글
        "toggles.block_recheck": False,
    }
    cfg = load_config(overrides=overrides)
    fake = FakeLLM(respond=voters("normal", "off_topic", "off_topic"))
    llm = LLM(cfg.llm, send=fake.send)
    await llm.start()
    j = await InputOnlyGuard(cfg, llm, labeled_rows(_public())).judge(req())
    assert (j.response.category, j.calls) == ("off_topic", 3)
    assert j.input_votes == "normal,off_topic,off_topic"


# --- input.block_veto: 1차가 탈옥·off_topic일 때만 B·C를 불러 둘 다 허용이면 푼다 ---


@pytest.mark.parametrize(
    ("first", "b", "c", "want"),
    [
        ("jailbreak_injection", "normal", "answer_seeking", "answer_seeking"),  # C의 범주
        ("off_topic", "normal", "normal", "normal"),
        ("off_topic", "normal", "off_topic", None),  # 하나라도 차단이면 그대로
        ("jailbreak_injection", "harmful_content", "normal", None),
        ("off_topic", None, "normal", None),  # 무효가 있으면 그대로
        ("off_topic", "normal", None, None),
        ("harmful_content", "normal", "normal", None),  # 대상 범주가 아니다
        ("academic_dishonesty", "normal", "normal", None),
        ("normal", "normal", "normal", None),
    ],
)
def test_veto_release(first, b, c, want):
    from guardrail.decide import veto_release

    assert veto_release(first, b, c, BLOCK) == want


def test_ensemble_and_block_veto_cannot_both_be_on():
    with pytest.raises(ValueError, match="block_veto"):
        load_config(overrides={"input.ensemble": True, "input.block_veto": True})


async def test_veto_not_triggered_for_allow_first(no_rule):
    guard, fake = await make(voters("normal", "off_topic", "off_topic"), input__block_veto=True)
    j = await guard.judge(req())
    assert j.response.category == "normal" and j.input_votes is None
    assert len(calls(fake, "input")) == 1 and j.calls == 2


async def test_veto_releases_and_skips_jb_check(no_rule):
    guard, fake = await make(
        voters("jailbreak_injection", "normal", "answer_seeking"), input__block_veto=True
    )
    j = await guard.judge(req())
    assert j.response.category == "answer_seeking" and j.response.output.safe is True
    assert j.input_votes == "jailbreak_injection,normal,answer_seeking"
    assert sorted(_voter(r) for r in calls(fake, "input")) == ["A", "B", "C"]
    assert calls(fake, "jb_check") == [] and (j.calls, j.errors) == (4, ())


async def test_veto_keeps_block_and_jb_check_runs_as_today(no_rule):
    guard, fake = await make(
        voters("jailbreak_injection", "normal", "off_topic", jb="apply"), input__block_veto=True
    )
    j = await guard.judge(req())
    assert j.response.category == "jailbreak_injection"
    assert len(calls(fake, "jb_check")) == 1 and j.calls == 5
    assert j.input_votes == "jailbreak_injection,normal,off_topic"


async def test_veto_release_runs_allow_path_checks(no_rule):
    # 풀린 off_topic은 차단 재확인 대신 허용 쪽 요청 점검(request_check)을 탄다
    guard, fake = await make(
        voters("off_topic", "normal", "normal"),
        input__block_veto=True,
        toggles__request_check=True,
        toggles__block_recheck=True,
    )
    j = await guard.judge(req())
    assert j.input_votes == "off_topic,normal,normal" and "request_check" in j.errors
    assert j.calls == 5  # A + B·C + 요청 점검 1회(가짜 응답이 스키마에 안 맞아 실패) + 출력


def test_block_veto_estimate_is_an_upper_bound():
    off = load_config()
    on = load_config(overrides=parse_sets(["input.block_veto=true"]))
    assert calls_per_row(on, True) == calls_per_row(off, True) + 2
