"""S3.8 예/아니오 칸 변형(기본 끔): nano가 범주 앞에 두 칸을 쓰고, 판정은 범주만 쓴다.

근거·결과: results/2026-11-16-input-flags.md(진짜 경로·제출 A/B에서 이득 없음, 미채택).
"""

import time

import pytest
from fakes import FakeLLM, Ok

from guardrail.config import load_config, load_prompt
from guardrail.eval.fake_llm import make_fake_llm
from guardrail.judges.input_judge import judge_input
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


async def test_flag_reply_uses_category_even_when_a_flag_is_true():
    got, req = await _judge(_reply("normal", offcourse=True, submit=True))
    assert got == "normal"
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
