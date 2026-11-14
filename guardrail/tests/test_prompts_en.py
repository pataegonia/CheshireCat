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
    _, ko = load_prompt(load_config(), name)
    _, en = load_prompt(load_config(overrides=EN), name)
    assert _ascii_marks(ko) <= _ascii_marks(en)


@pytest.mark.parametrize("name", NAMES)
def test_default_stays_korean_v1(name):
    version, body = load_prompt(load_config(), name)
    assert version == "1"
    assert body.startswith("너는")


def test_english_prompts_change_the_config_hash():
    assert config_hash(load_config(overrides=EN)) != config_hash(load_config())
