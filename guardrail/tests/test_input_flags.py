"""S3.8 예/아니오 칸 변형: nano가 범주 앞에 두 칸을 쓰고, 허용 범주인데 칸이 참이면 코드가 올린다.

근거: 2.1 선별에서 nano가 evidence에 산출물(광고 문구·그림·제출용 코드)을 맞게 적고도
normal·answer_seeking을 냈다(plan §15).
"""

import time

import pytest
from fakes import FakeLLM, Ok

from guardrail.config import load_config, load_prompt
from guardrail.eval.fake_llm import make_fake_llm
from guardrail.judges.input_judge import judge_input, lift_category
from guardrail.judges.schemas import InputFlagOut, strict_schema
from guardrail.llm import LLM
from guardrail.schema import Message

pytestmark = pytest.mark.anyio

FLAGS = {
    "prompts.input_judge": "input_judge_en_flags.md",
    "input.label_mode": "flags",
    "toggles.knn_fewshot": False,
    "toggles.jb_check": False,
}
MSGS = [Message("user", "질문")]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _model_env(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "test-model")


def _reply(category: str, offcourse: bool = False, submit: bool = False) -> str:
    o, s = str(offcourse).lower(), str(submit).lower()
    return (
        f'{{"evidence": "e", "makes_offcourse_output": {o}, '
        f'"makes_work_to_submit": {s}, "category": "{category}"}}'
    )


async def _judge(reply: str, overrides=FLAGS):
    cfg = load_config(overrides=overrides)
    fake = FakeLLM(respond=lambda kw: Ok(reply))
    llm = LLM(cfg.llm, send=fake.send)
    await llm.start()
    got = await judge_input(MSGS, "질문", [], llm, cfg, deadline=time.monotonic() + 30)
    return got, fake.requests[-1]


def test_flag_schema_puts_both_flags_after_evidence_and_before_category():
    schema = strict_schema(InputFlagOut)
    assert list(schema["properties"]) == [
        "evidence",
        "makes_offcourse_output",
        "makes_work_to_submit",
        "category",
    ]
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize(
    ("category", "offcourse", "submit", "want"),
    [
        ("normal", True, False, "off_topic"),
        ("normal", False, True, "academic_dishonesty"),
        ("answer_seeking", False, True, "academic_dishonesty"),
        ("pii_disclosure", True, False, "off_topic"),  # 자기소개 + 차단 요청은 차단(우선순위)
        ("normal", True, True, "academic_dishonesty"),  # 둘 다면 제출물 쪽
        ("normal", False, False, "normal"),
        # 이미 차단이거나 따로 다루는 범주는 그대로
        ("harmful_content", True, False, "harmful_content"),
        ("jailbreak_injection", False, True, "jailbreak_injection"),
        ("emotional_distress", True, False, "emotional_distress"),
        ("out_of_material", True, False, "out_of_material"),
        ("low_signal_input", True, True, "low_signal_input"),
    ],
)
def test_lift_category(category, offcourse, submit, want):
    assert lift_category(category, offcourse, submit) == want


async def test_flag_reply_lifts_allowed_category():
    got, req = await _judge(_reply("normal", offcourse=True))
    assert got == "off_topic"
    assert "InputFlagOut" in str(req.get("response_format"))


async def test_flag_reply_without_flags_keeps_category():
    got, _ = await _judge(_reply("answer_seeking"))
    assert got == "answer_seeking"


async def test_category_only_reply_is_rejected_in_flag_mode():
    got, _ = await _judge('{"evidence": "e", "category": "off_topic"}')
    assert got is None


def test_flag_prompt_defines_both_flags_in_output_order():
    version, body = load_prompt(load_config(overrides=FLAGS), "input_judge")
    assert version.endswith("flags")
    out = body.split("## Output")[1]
    order = ["`evidence`", "`makes_offcourse_output`", "`makes_work_to_submit`", "`category`"]
    assert [out.index(f) for f in order] == sorted(out.index(f) for f in order)


async def test_fake_llm_answers_the_flag_schema():
    cfg = load_config(overrides=FLAGS)
    llm = make_fake_llm(cfg.llm)
    await llm.start()
    got = await judge_input(MSGS, "질문", [], llm, cfg, deadline=time.monotonic() + 30)
    assert got == "normal"
