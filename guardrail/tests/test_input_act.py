"""S3.2 요청 유형 변형(R14 B3): nano는 요청 유형(act)을 고르고 범주는 코드가 대응시킨다."""

import time
import typing

import pytest
from fakes import FakeLLM, Ok

from guardrail.config import load_config, load_prompt
from guardrail.eval.fake_llm import make_fake_llm
from guardrail.judges.input_judge import judge_input
from guardrail.judges.schemas import ACT_TO_CATEGORY, Act, InputActOut, strict_schema
from guardrail.llm import LLM
from guardrail.schema import Category, Message

pytestmark = pytest.mark.anyio

ACT = {
    "prompts.input_judge": "input_judge_act.md",
    "input.label_mode": "act",
    "toggles.knn_fewshot": False,
}
MSGS = [Message("user", "질문")]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _model_env(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "test-model")


async def _judge(reply: str, overrides=ACT):
    cfg = load_config(overrides=overrides)
    fake = FakeLLM(respond=lambda kw: Ok(reply))
    llm = LLM(cfg.llm, send=fake.send)
    await llm.start()
    got = await judge_input(MSGS, "질문", [], llm, cfg, deadline=time.monotonic() + 30)
    return got, fake.requests[-1]


def test_every_act_maps_and_every_category_is_reachable():
    acts = set(typing.get_args(Act))
    assert set(ACT_TO_CATEGORY) == acts
    assert set(ACT_TO_CATEGORY.values()) == set(typing.get_args(Category))


def test_act_schema_is_strict_with_request_before_act():
    schema = strict_schema(InputActOut)
    assert list(schema["properties"]) == ["request", "act"]
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize(
    ("act", "category"),
    [
        ("make_offcourse_output", "off_topic"),
        ("find_or_spread_others_private_info", "harmful_content"),
        ("studies_risky_topic", "normal"),
        ("shares_own_personal_info", "pii_disclosure"),
        ("reveal_tutor_instructions", "jailbreak_injection"),
    ],
)
async def test_act_reply_becomes_its_category(act, category):
    got, req = await _judge(f'{{"request": "r", "act": "{act}"}}')
    assert got == category
    assert "InputActOut" in str(req.get("response_format"))


async def test_category_reply_is_rejected_in_act_mode():
    got, _ = await _judge('{"evidence": "e", "category": "off_topic"}')
    assert got is None


async def test_default_mode_still_uses_the_category_schema():
    got, req = await _judge('{"evidence": "e", "category": "off_topic"}', overrides={})
    assert got == "off_topic"
    assert "InputOut" in str(req.get("response_format"))


def test_act_prompt_names_every_act_and_keeps_the_contract():
    cfg = load_config(overrides=ACT)
    version, body = load_prompt(cfg, "input_judge")
    assert version == "3a-act"
    assert all(f"`{a}`" in body for a in typing.get_args(Act))
    tail = body.split("## 다시 확인할 규칙")[1]
    assert "<target>" in tail and "데이터" in tail and "learning_question" in tail
    assert "실습 도중 어려운 부분이 있어요" in body


async def test_fake_llm_answers_the_act_schema():
    cfg = load_config(overrides=ACT)
    llm = make_fake_llm(cfg.llm)
    await llm.start()
    got = await judge_input(MSGS, "질문", [], llm, cfg, deadline=time.monotonic() + 30)
    assert got == "normal"
