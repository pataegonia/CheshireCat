"""영어 지시문 실험판(R14-D9): `prompts/*_en.md`는 --set으로만 고르고 기본값은 한국어 v1이다."""

import re

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
