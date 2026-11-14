"""S3.2 입력 변형 스위치(R14 B4·B5·B13): 예시 순서, kNN sublinear_tf, 변형 프롬프트 파일."""

import re

import pytest
from pydantic import ValidationError

from guardrail.config import load_config, load_prompt
from guardrail.data import LabeledRow, labeled_rows, load_public
from guardrail.judges.input_judge import build_messages
from guardrail.knn import KnnIndex
from guardrail.schema import Message

CATEGORIES = (
    "normal answer_seeking academic_dishonesty off_topic out_of_material "
    "jailbreak_injection pii_disclosure harmful_content emotional_distress low_signal_input"
).split()
FAR = LabeledRow("a", "guard", "먼 예시", "normal", ())
NEAR = LabeledRow("b", "guard", "가까운 예시", "off_topic", ())
V2A = {"prompts.input_judge": "input_judge_v2a.md"}


def _shown(cfg):
    user = build_messages([Message("user", "질문")], "질문", [FAR, NEAR], cfg)[1]["content"]
    return re.findall(r"<example category=\"[a-z_]+\">(.*?)</example>", user)


def test_examples_default_order_keeps_nearest_last():
    assert _shown(load_config()) == ["먼 예시", "가까운 예시"]


def test_examples_nearest_first_reverses_the_given_order():
    cfg = load_config(overrides={"input.knn.order": "nearest_first", **V2A})
    assert _shown(cfg) == ["가까운 예시", "먼 예시"]


def test_unknown_example_order_is_rejected_at_load():
    with pytest.raises((ValidationError, ValueError)):
        load_config(overrides={"input.knn.order": "shuffled"})


def test_knn_sublinear_tf_follows_config():
    rows = labeled_rows(load_public(load_config().paths.public))[:40]
    assert KnnIndex(rows, load_config())._vec.sublinear_tf is False
    on = load_config(overrides={"input.knn.sublinear_tf": True})
    assert KnnIndex(rows, on)._vec.sublinear_tf is True


def test_variant_prompt_v2a_loads_and_keeps_the_contract():
    cfg = load_config(overrides=V2A)
    version, body = load_prompt(cfg, "input_judge")
    assert version == "2a"
    assert all(f"`{c}`" in body for c in CATEGORIES)  # 10개 범주를 모두 정의한다
    # GR-04 샌드위치 3규칙과 버튼 1턴 → normal(SPEC §2 문구)
    tail = body.split("## 다시 확인할 규칙")[1]
    assert "<target>" in tail and "데이터" in tail and "normal" in tail
    assert "실습 도중 어려운 부분이 있어요" in body
    system = build_messages([Message("user", "질문")], "질문", [], cfg)[0]["content"]
    assert system == body
    assert system != load_prompt(load_config(), "input_judge")[1]  # v1은 그대로다
