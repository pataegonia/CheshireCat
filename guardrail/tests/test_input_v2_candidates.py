"""S3.2 v2 후보(C3-tuning §S3.2): C2 판별 속성 `mode`, C3 v1 최소 수정, 둘을 합친 C23."""

import re
import time
import typing

import pytest
from fakes import FakeLLM, Ok

from guardrail.config import load_config, load_prompt, resolve_path
from guardrail.data import load_public
from guardrail.eval import fake_llm
from guardrail.eval.fake_llm import make_fake_llm
from guardrail.judges.input_judge import judge_input
from guardrail.judges.schemas import InputModeOut, strict_schema
from guardrail.llm import LLM
from guardrail.preprocess import normalize
from guardrail.schema import Category, Message

pytestmark = pytest.mark.anyio

C2 = {"prompts.input_judge": "input_judge_c2.md", "input.label_mode": "mode"}
C3 = {"prompts.input_judge": "input_judge_c3.md"}
C23 = {"prompts.input_judge": "input_judge_c23.md", "input.label_mode": "mode"}
VARIANTS = {"c2": C2, "c3": C3, "c23": C23}
MSGS = [Message("user", "질문")]
# v1 프롬프트와 공개 문항이 공백을 빼고 연속으로 겹치는 가장 긴 길이가 11자다. 그보다 길면
# 문항 문구를 옮겨 적은 것으로 본다(공개 세트 과적합, C3-tuning §S3.2)
# (필드별 최댓값, 프롬프트 파일 전체 기준: 학습자 발화 11, 튜터 턴 11, reference_answer 11,
# 후보 답 9, gold_facts 6. 학습자·튜터·reference는 문턱과 같아 여유가 없다)
MAX_SHARED_CHARS = 11
# SPEC §2 빠른 질문 버튼 문구. 프롬프트가 일부러 그대로 인용하므로(범주 1) 겹침 비교에서만 뺀다
QUICK_BUTTONS = (
    "실습 도중 어려운 부분이 있어요. 도움을 받을 수 있을까요?",
    "이 퀴즈를 푸는 데 힌트를 받을 수 있을까요?",
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _model_env(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "test-model")


async def _judge(reply: str, overrides):
    cfg = load_config(overrides=overrides)
    fake = FakeLLM(respond=lambda kw: Ok(reply))
    llm = LLM(cfg.llm, send=fake.send)
    await llm.start()
    got = await judge_input(MSGS, "질문", [], llm, cfg, deadline=time.monotonic() + 30)
    return got, fake.requests[-1]


def test_mode_schema_is_strict_with_mode_between_evidence_and_category():
    schema = strict_schema(InputModeOut)
    assert list(schema["properties"]) == ["evidence", "mode", "category"]
    assert schema["additionalProperties"] is False
    assert set(typing.get_args(InputModeOut.model_fields["mode"].annotation)) == {
        "use",
        "mention",
        "none",
    }


async def test_mode_reply_returns_the_category_nano_chose():
    got, req = await _judge('{"evidence": "e", "mode": "use", "category": "off_topic"}', C2)
    assert got == "off_topic"
    assert "InputModeOut" in str(req.get("response_format"))


@pytest.mark.parametrize(
    ("mode", "category"),
    [("mention", "jailbreak_injection"), ("use", "normal"), ("none", "harmful_content")],
)
async def test_code_never_overrides_the_category_from_mode(mode, category):
    # R14-D5: 속성에서 코드가 block을 도출하지 않는다. 어긋나도 nano의 범주 그대로다
    reply = f'{{"evidence": "e", "mode": "{mode}", "category": "{category}"}}'
    got, _ = await _judge(reply, C2)
    assert got == category


async def test_reply_without_mode_is_rejected_in_mode_mode():
    reply = '{"evidence": "e", "category": "off_topic"}'
    assert (await _judge(reply, {}))[0] == "off_topic"  # 대조: 기본 스키마에서는 받아들인다
    assert (await _judge(reply, C2))[0] is None


async def test_default_mode_still_uses_the_v1_schema():
    got, req = await _judge('{"evidence": "e", "category": "off_topic"}', {})
    assert got == "off_topic"
    assert "InputOut" in str(req.get("response_format"))


async def test_fake_llm_answers_the_mode_schema():
    cfg = load_config(overrides=C2)
    llm = make_fake_llm(cfg.llm)
    await llm.start()
    got = await judge_input(MSGS, "질문", [], llm, cfg, deadline=time.monotonic() + 30)
    assert got == "normal"
    InputModeOut.model_validate(fake_llm.MODE_REPLY)
    kwargs = {"response_format": {"json_schema": {"name": "InputModeOut"}}, "messages": []}
    assert fake_llm._reply(kwargs) == fake_llm.MODE_REPLY


@pytest.mark.parametrize("name", VARIANTS)
def test_variant_prompt_loads_and_keeps_the_contract(name):
    cfg = load_config(overrides=VARIANTS[name])
    version, body = load_prompt(cfg, "input_judge")
    assert version == f"2-{name}"
    assert all(f"`{c}`" in body for c in typing.get_args(Category))
    # GR-04 샌드위치 3규칙과 버튼 1턴 → normal(SPEC §2 문구)
    tail = body.split("## 다시 확인할 규칙")[1]
    assert "<target>" in tail and "데이터" in tail and "normal" in tail
    assert "실습 도중 어려운 부분이 있어요" in body
    assert '그 안에 "시스템 지시"' in body  # 태그 안은 데이터(주입 저항 문단)
    assert body != load_prompt(load_config(), "input_judge")[1]  # v1은 그대로다


@pytest.mark.parametrize(("name", "has_mode"), [("c2", True), ("c3", False), ("c23", True)])
def test_only_mode_variants_ask_for_the_mode_field(name, has_mode):
    body = load_prompt(load_config(overrides=VARIANTS[name]), "input_judge")[1]
    output = body.split("## 출력")[1].split("## 다시 확인할 규칙")[0]
    assert ("`mode`" in output) is has_mode
    assert all((f"`{v}`" in output) is has_mode for v in ("use", "mention", "none"))


def test_c2_changes_only_the_output_section_of_v1():
    v1 = load_prompt(load_config(), "input_judge")[1]
    c2 = load_prompt(load_config(overrides=C2), "input_judge")[1]
    assert c2.split("## 출력")[0] == v1.split("## 출력")[0]
    assert c2.split("## 다시 확인할 규칙")[1] == v1.split("## 다시 확인할 규칙")[1]


def test_c3_rewrites_the_conflicting_sentences():
    body = load_prompt(load_config(overrides=C3), "input_judge")[1]
    assert "튜터의 시스템 프롬프트" in body  # M1: 한정
    assert "문장 끝이" not in body  # M2: 부딪히던 문장
    assert "아래는 범주 단서가" not in body and "거절하지 말고" not in body  # M8
    assert body.count("실습 대상") <= 1  # M3: 같은 경계를 한 곳에서만
    assert "요구가 채워" not in body
    c2 = load_prompt(load_config(overrides=C2), "input_judge")[1]
    assert "요구가 채워" not in c2.split("## 출력")[1]


def _public_texts(cfg) -> list[str]:
    """공개 문항의 모든 텍스트 필드: 모든 발화(학습자·튜터), 후보 답, 참조 답, gold_facts."""
    out: list[str] = []
    for row in load_public(cfg.paths.public):
        out += [m.content for m in row.messages]
        out.append(row.candidate_answer or "")
        out.append(row.labels.get("reference_answer") or "")
        out += row.labels.get("gold_facts") or []
    return out


def test_no_input_prompt_copies_public_item_wording():
    cfg = load_config()
    squeeze = re.compile(r"\s+")

    def prep(text: str) -> str:
        return squeeze.sub("", normalize(text))

    texts = [t for t in map(prep, _public_texts(cfg)) if t]
    quick = [prep(q) for q in QUICK_BUTTONS]
    k = MAX_SHARED_CHARS + 1
    # 문항 쪽 k-gram을 한 번만 만든다
    items = {t[i : i + k] for t in texts for i in range(len(t) - k + 1)}
    for path in sorted(resolve_path(cfg.paths.prompts).glob("input_judge*.md")):
        body = prep(path.read_text("utf-8"))
        for q in quick:
            body = body.replace(q, "")
        copied = next(
            (body[i : i + k] for i in range(len(body) - k + 1) if body[i : i + k] in items), None
        )
        assert copied is None, f"{path.name}: 공개 문항과 {k}자 이상 겹친다"


# --- 프롬프트와 label_mode 짝 검사 ---

ACT = {"prompts.input_judge": "input_judge_act.md", "input.label_mode": "act"}
V1_MODE = {"input.label_mode": "mode"}
V1_ACT = {"input.label_mode": "act"}


@pytest.mark.parametrize(
    "overrides",
    [
        {"prompts.input_judge": "input_judge_c2.md", "input.label_mode": "category"},
        V1_MODE,
        {"prompts.input_judge": "input_judge_act.md", "input.label_mode": "category"},
        V1_ACT,
        {"prompts.input_judge": "input_judge_c3.md", "input.label_mode": "mode"},
    ],
)
def test_prompt_label_mode_mismatch_fails_at_load(overrides):
    with pytest.raises(ValueError, match="label_mode"):
        load_prompt(load_config(overrides=overrides), "input_judge")


@pytest.mark.parametrize("overrides", [{}, C2, C3, C23, ACT])
def test_matching_prompt_label_mode_loads(overrides):
    load_prompt(load_config(overrides=overrides), "input_judge")
