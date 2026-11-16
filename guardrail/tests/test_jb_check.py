"""S3.3 탈옥 2차 판정: 1차가 jailbreak_injection이면 적용인가 다루는 대상인가만 다시 묻는다."""

import json
import time

import pytest
from fakes import FakeLLM, Malformed, Ok, input_reply

from guardrail.config import load_config, load_prompt
from guardrail.judges.input_judge import judge_input
from guardrail.judges.schemas import JbCheckOut, strict_schema
from guardrail.llm import LLM
from guardrail.schema import Message

pytestmark = pytest.mark.anyio

ON = {"toggles.jb_check": True, "toggles.knn_fewshot": False}
MSGS = [Message("user", "질문")]
JB = input_reply("jailbreak_injection")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _model_env(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "test-model")


async def _judge(replies, overrides=ON):
    """replies[0] = 1차 응답, 나머지 = 2차 응답들(차례로). probe 요청은 1차 응답으로 받는다."""
    cfg = load_config(overrides=overrides)
    second = iter(replies[1:])

    def respond(kw):
        return next(second) if "JbCheckOut" in str(kw.get("response_format")) else replies[0]

    fake = FakeLLM(respond=respond)
    llm = LLM(cfg.llm, send=fake.send)
    await llm.start()
    fake.requests.clear()
    got = await judge_input(MSGS, "질문", [], llm, cfg, deadline=time.monotonic() + 30)
    return got, fake.requests


def test_check_schema_is_strict_with_evidence_first():
    schema = strict_schema(JbCheckOut)
    assert list(schema["properties"]) == ["evidence", "verdict"]
    assert schema["properties"]["verdict"]["enum"] == ["apply", "study"]
    assert schema["additionalProperties"] is False


async def test_study_verdict_turns_jailbreak_into_normal():
    got, reqs = await _judge([Ok(JB), Ok('{"evidence": "e", "verdict": "study"}')])
    assert got == "normal"
    assert len(reqs) == 2
    assert "JbCheckOut" in str(reqs[1].get("response_format"))
    assert (
        reqs[1]["messages"][0]["content"] == load_prompt(load_config(overrides=ON), "jb_check")[1]
    )
    # 2차도 같은 대화·대상 발화를 본다(1차의 few-shot 예시는 넣지 않는다)
    user = reqs[1]["messages"][1]["content"]
    assert "<target>질문</target>" in user and "<examples>" not in user


async def test_apply_verdict_keeps_jailbreak():
    got, reqs = await _judge([Ok(JB), Ok('{"evidence": "e", "verdict": "apply"}')])
    assert got == "jailbreak_injection" and len(reqs) == 2


async def test_failed_check_keeps_jailbreak():
    got, reqs = await _judge([Ok(JB), Malformed(), Malformed()])
    assert got == "jailbreak_injection"


async def test_other_categories_are_not_checked():
    got, reqs = await _judge([Ok(input_reply("off_topic"))])
    assert got == "off_topic" and len(reqs) == 1


async def test_check_off_keeps_one_call():
    off = {"toggles.jb_check": False, "toggles.knn_fewshot": False}
    got, reqs = await _judge([Ok(JB)], overrides=off)
    assert got == "jailbreak_injection" and len(reqs) == 1


def test_check_prompt_keeps_the_contract():
    version, body = load_prompt(load_config(), "jb_check")
    assert version == "1.1"
    assert "<target>" in body and "apply" in body and "study" in body
    assert "data" in body.lower()
    json.dumps(body)  # 직렬화 가능한 평문
