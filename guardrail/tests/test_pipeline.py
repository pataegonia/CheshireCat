"""S0.2 파이프라인 골격: 무방비 respond·check_*, Verdict 불변식, request_id, system_extra."""

import uuid

import pytest
from fakes import FakeTutor

from guardrail import Guard, Session, StageResult, Verdict
from guardrail.config import ROOT, load_config
from guardrail.types import STAGES, Chunk, Turn

pytestmark = pytest.mark.anyio

UNGUARDED = ROOT / "configs" / "unguarded.yaml"
INPUT_STAGES = ("normalize", "crisis", "rules", "pii", "jev", "input_judge")
OUTPUT_STAGES = ("output_code", "prefilter", "exchange_judge", "repair")
GUARD_STAGES = set(STAGES) - {"retrieve", "generate"}


@pytest.fixture
def guard() -> Guard:
    return Guard.from_config(UNGUARDED)


def _verdict(**kw) -> Verdict:
    base = dict(
        verdict="pass",
        action="allow",
        category="S0",
        confidence=None,
        rule_id=None,
        reason_code="x",
        reason="",
        latency_ms=0.0,
        policy_version="0.1.0",
        request_id="r",
        response=None,
    )
    return Verdict(**(base | kw))


# --- 타입 ---


def test_stages_are_the_13_contract_names():
    assert STAGES == (
        "normalize",
        "crisis",
        "rules",
        "pii",
        "jev",
        "input_judge",
        "retrieve",
        "generate",
        "retrieval_guard",
        "output_code",
        "prefilter",
        "exchange_judge",
        "repair",
    )


def test_session_defaults():
    s = Session()
    assert (s.user_id, s.grade_band, s.risk, s.last_block_norm) == (None, None, 0.0, None)


def test_verdict_has_gr11_common_fields():
    common = (
        "verdict action category confidence rule_id reason_code reason latency_ms "
        "policy_version response"
    ).split()
    fields = Verdict.__dataclass_fields__
    assert all(name in fields for name in common)
    assert {"request_id", "crisis_tier", "crisis_flag", "instruction"} <= set(fields)
    assert "config_hash" not in fields


# --- Verdict 불변식 ---


def test_modified_redirect_with_instruction_only_is_legal():
    # 방식 B check_input 전환·부분 응답: response=None, instruction 있음
    for action in ("redirect", "partial"):
        v = _verdict(verdict="modified", action=action, instruction="개념 설명으로 돌려라")
        assert v.response is None


def test_pass_with_no_response_is_legal():
    assert _verdict().response is None  # check_input 허용


@pytest.mark.parametrize(
    "kw",
    [
        dict(verdict="block", action="refuse"),  # 응답도 지시도 없는 차단
        dict(verdict="modified", action="refuse", instruction="x", response="r"),
        dict(verdict="block", action="redirect", instruction="x", response="r"),
        dict(verdict="pass", action="allow", instruction="x", response="r"),
        dict(verdict="block", action="crisis", response="위기 템플릿"),  # 등급 없음
    ],
)
def test_illegal_verdicts_raise(kw):
    with pytest.raises(ValueError):
        _verdict(**kw)


def test_crisis_with_tier_is_legal():
    v = _verdict(verdict="block", action="crisis", crisis_tier="T1", response="위기 템플릿")
    assert v.crisis_tier == "T1"


# --- 무방비 respond ---


async def test_unguarded_respond_returns_tutor_answer_unchanged(guard):
    tutor = FakeTutor(answer="승화는 고체가 바로 기체가 되는 것이다. [S1]")
    v = await guard.respond("승화가 뭐야?", [], tutor)
    assert v.response == tutor.answer
    assert (v.verdict, v.action, v.category, v.reason_code) == ("pass", "allow", "S0", "stages_off")
    assert v.policy_version == guard.cfg.policy_version
    assert v.latency_ms >= 0


async def test_unguarded_respond_records_13_stages(guard):
    v = await guard.respond("질문", [], FakeTutor())
    names = [s.stage for s in v.stages]
    assert sorted(names) == sorted(STAGES) and len(names) == 13
    for s in v.stages:
        if s.stage in ("retrieve", "generate"):
            assert s.status == "ok"
        else:
            assert s.status == "skipped" and s.reason_code == "disabled"
            assert s.action is None and s.latency_ms == 0.0
    assert {s.stage for s in v.stages if s.status == "skipped"} == GUARD_STAGES


async def test_generate_stage_chars_and_zero_tokens(guard):
    tutor = FakeTutor(answer="열두 글자 답이다 [S1]")
    v = await guard.respond("질문", [], tutor)
    gen = next(s for s in v.stages if s.stage == "generate")
    assert gen.detail == {"chars": len(tutor.answer)}
    assert gen.tokens_in == gen.tokens_out == 0
    assert gen.started_ms >= next(s for s in v.stages if s.stage == "retrieve").started_ms


async def test_unguarded_tutor_gets_empty_system_extra(guard):
    tutor = FakeTutor()
    history = [Turn("student", "안녕"), Turn("tutor", "안녕하세요")]
    await guard.respond("질문", history, tutor)
    call = tutor.generate_calls[0]
    assert call["system_extra"] == "" and call["instruction"] is None
    assert call["history"] == history and call["question"] == "질문"
    assert tutor.retrieve_calls == [("질문", history)]
    assert call["chunks"] == tutor.chunks


def test_unguarded_system_extra_is_empty(guard):
    assert guard.system_extra() == ""
    assert guard.system_extra(guard.new_canary()) == ""
    assert guard.new_canary() is None


async def test_request_id_is_fresh_uuid4_per_call(guard):
    tutor = FakeTutor()
    a = await guard.respond("q", [], tutor)
    b = await guard.respond("q", [], tutor)
    c = await guard.check_input("q", [])
    ids = {a.request_id, b.request_id, c.request_id}
    assert len(ids) == 3
    assert all(uuid.UUID(i).version == 4 for i in ids)


async def test_respond_accepts_session_and_tutor_errors_propagate(guard):
    class Broken(FakeTutor):
        async def generate(self, *a, **kw):
            raise RuntimeError("tutor down")

    v = await guard.respond("q", [], FakeTutor(), session=Session(grade_band="중1"))
    assert v.response
    with pytest.raises(RuntimeError):
        await guard.respond("q", [], Broken())


async def test_adapter_without_system_prompt_attribute(guard):
    class Minimal:
        async def retrieve(self, question, history):
            return [Chunk("S1", "src", "본문")]

        async def generate(self, question, history, chunks, *, system_extra="", instruction=None):
            return "답 [S1]"

    assert (await guard.respond("q", [], Minimal())).response == "답 [S1]"


# --- 무방비 check_input · guard_retrieval · check_output ---


async def test_unguarded_check_input(guard):
    v = await guard.check_input("질문", [])
    assert (v.verdict, v.action, v.response, v.instruction) == ("pass", "allow", None, None)
    assert [s.stage for s in v.stages] == list(INPUT_STAGES)
    assert all(s.status == "skipped" for s in v.stages)


def test_unguarded_guard_retrieval_returns_same_chunks(guard):
    chunks = [Chunk("S1", "a", "x"), Chunk("S2", "b", "y")]
    assert guard.guard_retrieval(chunks) == chunks


async def test_unguarded_check_output_copies_input_stages(guard):
    iv = await guard.check_input("질문", [])
    iv.crisis_flag = True  # 입력 쪽 플래그가 출력 쪽으로 넘어가는지
    chunks = [Chunk("S1", "a", "x")]
    ov = await guard.check_output("질문", [], chunks, "답 [S1]", iv)
    assert ov.response == "답 [S1]" and ov.verdict == "pass" and ov.action == "allow"
    assert ov.crisis_flag is True
    assert [s.stage for s in ov.stages] == list(INPUT_STAGES) + list(OUTPUT_STAGES)
    assert ov.stages[: len(iv.stages)] == iv.stages
    assert ov.request_id != iv.request_id


async def test_check_output_accepts_contract_keywords(guard):
    iv = await guard.check_input("q", [])
    ov = await guard.check_output(
        "q", [], [], "a", iv, regenerate=None, canary=None, system_prompt="sys"
    )
    assert ov.response == "a"


async def test_finalize_runs_on_all_three_paths(guard, monkeypatch):
    seen: list[tuple[bool, object]] = []
    original = guard._finalize

    def spy(verdict, crisis_flag, crisis_tier):
        seen.append((crisis_flag, crisis_tier))
        return original(verdict, crisis_flag, crisis_tier)

    monkeypatch.setattr(guard, "_finalize", spy)
    iv = await guard.check_input("q", [])
    await guard.check_output("q", [], [], "a", iv)
    await guard.respond("q", [], FakeTutor())
    assert len(seen) == 3


def test_from_config_overrides():
    g = Guard.from_config(UNGUARDED, overrides={"thresholds.G": 0.6})
    assert g.cfg.thresholds.G == 0.6


def test_guard_accepts_config_object():
    cfg = load_config(UNGUARDED)
    assert Guard(cfg).cfg is cfg


def test_stage_result_defaults():
    s = StageResult("rules", None, None, None, None, "disabled", 0.0)
    assert (s.status, s.attempts, s.fallback_infra, s.detail) == ("ok", 1, False, {})
