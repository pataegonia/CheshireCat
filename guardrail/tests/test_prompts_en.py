"""영어 지시문판(R14-D9): `prompts/*_en.md`가 기본이고 한국어 v1은 --set으로 고른다."""

import json
import re
from pathlib import Path

import pytest

from guardrail.config import config_hash, load_config, load_prompt

NAMES = ("input_judge", "output_judge", "output_judge_baseline")
EN = {f"prompts.{n}": f"{n}_en.md" for n in NAMES}


def _ascii_marks(body: str) -> set[str]:
    """백틱 안의 ASCII 표기(범주 id·스키마 필드·태그·예시 섹션 ID). 번역해도 그대로여야 한다."""
    return {m for m in re.findall(r"`([^`]+)`", body) if m.isascii()}


@pytest.mark.parametrize("name", NAMES)
def test_english_prompt_loads_with_expected_version(name):
    version, body = load_prompt(load_config(overrides=EN), name)
    assert version == ("2.0" if name == "input_judge" else "1.1")
    assert body.startswith("You ")


@pytest.mark.parametrize("name", NAMES)
def test_english_prompt_keeps_every_identifier(name):
    _, ko = load_prompt(load_config(overrides={f"prompts.{name}": f"{name}.md"}), name)
    _, en = load_prompt(load_config(overrides=EN), name)
    assert _ascii_marks(ko) <= _ascii_marks(en)


@pytest.mark.parametrize("name", ["input_judge", "output_judge"])
def test_default_uses_english_prompts(name):
    """R14-D9 채택: 기본 입력·출력 판정은 영어판이다. 입력은 2.0(S3.2), 출력은 1.4(S3.10)."""
    version, body = load_prompt(load_config(), name)
    assert version == ("2.0" if name == "input_judge" else "1.4")
    assert body.startswith("You ")


@pytest.mark.parametrize("config", ["configs/default.yaml", "configs/baseline.yaml"])
def test_baseline_output_prompt_stays_korean_v1(config):
    """출력 베이스라인 프롬프트는 영어로 재지 않았다. 입력은 기본과 같은 파일이다(불변식)."""
    assert load_prompt(load_config(config), "output_judge_baseline")[0] == "1"


@pytest.mark.parametrize("name", ["input_judge", "output_judge"])
def test_korean_v1_stays_selectable(name):
    version, body = load_prompt(load_config(overrides={f"prompts.{name}": f"{name}.md"}), name)
    assert version == "1"
    assert body.startswith("너는")


def test_english_prompts_change_the_config_hash():
    ko = {f"prompts.{n}": f"{n}.md" for n in NAMES}
    assert config_hash(load_config(overrides=EN)) != config_hash(load_config(overrides=ko))


# --- input_judge_en.md 2.0 (S3.2) ---

CATEGORIES = (
    "normal", "answer_seeking", "academic_dishonesty", "off_topic", "out_of_material",
    "jailbreak_injection", "pii_disclosure", "harmful_content", "emotional_distress",
    "low_signal_input",
)  # fmt: skip
HEDGES = ("generally", "usually", "mostly", "typically", "often", "step by step")
PUBLIC = Path(__file__).resolve().parents[2] / "competition" / "public"
WINDOW = 8
BOUNDARY_MAX_TOKENS = 1300
# 빠른 질문 버튼 문구는 공개 행에 그대로 있다(정의 1). 이 둘만 누출 검사에서 뺀다
BUTTONS = (
    "실습 도중 어려운 부분이 있어요. 도움을 받을 수 있을까요?",
    "이 퀴즈를 푸는 데 힌트를 받을 수 있을까요?",
)


def _en() -> str:
    return load_prompt(load_config(), "input_judge")[1]


def _boundary(body: str) -> str:
    return body.split("## How to draw the boundaries")[1].split("## Output")[0]


def _tokens(text: str) -> list[str]:
    return [t for t in (re.sub(r"[^\w]", "", w) for w in text.split()) if t]


def _windows(text: str) -> set[tuple[str, ...]]:
    t = _tokens(text)
    return {tuple(t[i : i + WINDOW]) for i in range(len(t) - WINDOW + 1)}


def _est_tokens(text: str) -> float:
    ko = len(re.findall(r"[ㄱ-ㆎ가-힣]", text))
    return ko / 1.5 + (len(text) - ko) / 3.5


def test_input_judge_en_is_2_0():
    version, body = load_prompt(load_config(overrides=EN), "input_judge")
    assert version == "2.0"
    assert body.startswith("You ")


def test_input_judge_en_keeps_categories_tags_and_output_order():
    body = _en()
    for cat in CATEGORIES:
        assert f"`{cat}`" in body
    for tag in ("<examples>", "<conversation>", "<target>", "<kb>"):
        assert tag in body
    out = body.split("## Output")[1]
    assert out.index("`evidence`") < out.index("`category`")


def test_input_judge_en_has_no_hedge_words():
    body = _en()
    for w in HEDGES:
        assert not re.search(rf"\b{w}\b", body, re.IGNORECASE), w


@pytest.mark.parametrize("name", ["input_judge", "jb_check"])
def test_prompt_does_not_leak_public_rows(name):
    # 8어절 창이라 2–4어절짜리 짧은 단서 조각(jb_check의 한국어 어미)은 잡지 못한다
    mine = _windows(load_prompt(load_config(), name)[1])
    for button in BUTTONS:
        mine -= _windows(button)
    assert mine
    seen = set()
    for path in PUBLIC.glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            texts = [m["content"] for m in row.get("messages", [])]
            texts.append(row.get("candidate_answer") or "")
            for text in texts:
                seen |= _windows(text) & mine
    assert not seen, sorted(seen)[:3]


def test_input_judge_en_boundary_section_stays_within_budget():
    assert _est_tokens(_boundary(_en())) <= BOUNDARY_MAX_TOKENS
