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
def test_english_prompt_loads_as_version_1_1(name):
    version, body = load_prompt(load_config(overrides=EN), name)
    assert version == "1.1"
    assert body.startswith("You ")


@pytest.mark.parametrize("name", NAMES)
def test_english_prompt_keeps_every_identifier(name):
    _, ko = load_prompt(load_config(overrides={f"prompts.{name}": f"{name}.md"}), name)
    _, en = load_prompt(load_config(overrides=EN), name)
    assert _ascii_marks(ko) <= _ascii_marks(en)


@pytest.mark.parametrize("name", ["input_judge", "output_judge"])
def test_default_uses_english_1_1(name):
    """R14-D9 채택(results/2026-11-14-v1-english.md): 기본 입력·출력 판정은 영어판이다."""
    version, body = load_prompt(load_config(), name)
    assert version == "1.1"
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


# --- input_judge_en_v4.md (2.0-guide, S3.2) ---

V4 = {"prompts.input_judge": "input_judge_en_v4.md"}
CATEGORIES = (
    "normal", "answer_seeking", "academic_dishonesty", "off_topic", "out_of_material",
    "jailbreak_injection", "pii_disclosure", "harmful_content", "emotional_distress",
    "low_signal_input",
)  # fmt: skip
HEDGES = ("generally", "usually", "mostly", "typically", "often", "step by step")
PUBLIC = Path(__file__).resolve().parents[2] / "competition" / "public"
WINDOW = 8


def _v4() -> str:
    return load_prompt(load_config(overrides=V4), "input_judge")[1]


def _en11() -> str:
    return load_prompt(load_config(overrides=EN), "input_judge")[1]


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


def test_v4_loads_as_input_judge_2_0_guide():
    version, body = load_prompt(load_config(overrides=V4), "input_judge")
    assert version == "2.0-guide"
    assert body.startswith("You ")
    assert load_prompt(load_config(), "input_judge")[0] == "1.1"  # 기본은 그대로


def test_v4_keeps_every_identifier_of_1_1():
    assert _ascii_marks(_en11()) <= _ascii_marks(_v4())


def test_v4_keeps_categories_tags_and_output_order():
    body = _v4()
    for cat in CATEGORIES:
        assert f"`{cat}`" in body
    for tag in ("<examples>", "<conversation>", "<target>", "<kb>"):
        assert tag in body
    out = body.split("## Output")[1]
    assert out.index("`evidence`") < out.index("`category`")


def test_v4_has_no_hedge_words():
    body = _v4()
    for w in HEDGES:
        assert not re.search(rf"\b{w}\b", body, re.IGNORECASE), w


def test_v4_does_not_leak_public_rows():
    mine = _windows(_v4()) - _windows(_en11())
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


def test_v4_boundary_section_stays_within_budget():
    assert _est_tokens(_boundary(_v4())) <= _est_tokens(_boundary(_en11())) + 900
