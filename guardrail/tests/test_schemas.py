"""S1.3 판정 출력 스키마: 필드 순서·extra 거부·strict 변환."""

from typing import get_args

import pytest
from pydantic import ValidationError

from guardrail.judges.schemas import InputOut, OutputBaselineOut, OutputOut, strict_schema
from guardrail.schema import Category

MODELS = {
    InputOut: ["evidence", "category"],
    OutputOut: ["reason", "grounded"],
    OutputBaselineOut: ["reason", "grounded", "safe"],
}


@pytest.mark.parametrize(("model", "order"), MODELS.items())
def test_field_order_is_fixed(model, order):
    assert list(model.model_fields) == order


@pytest.mark.parametrize(
    ("model", "data"),
    [
        (InputOut, {"evidence": "e", "category": "normal"}),
        (OutputOut, {"reason": "r", "grounded": True}),
        (OutputBaselineOut, {"reason": "r", "grounded": True, "safe": False}),
    ],
)
def test_rejects_unknown_and_missing_keys(model, data):
    assert model.model_validate(data)
    with pytest.raises(ValidationError):
        model.model_validate({**data, "extra": 1})
    with pytest.raises(ValidationError):
        model.model_validate({k: v for k, v in data.items() if k != "reason" and k != "evidence"})


def test_category_outside_enum_rejected():
    with pytest.raises(ValidationError):
        InputOut.model_validate({"evidence": "e", "category": "nope"})


@pytest.mark.parametrize(("model", "order"), MODELS.items())
def test_strict_schema(model, order):
    s = strict_schema(model)
    assert s["additionalProperties"] is False
    assert s["required"] == order
    assert list(s["properties"]) == order


def test_strict_schema_category_enum_matches_type():
    assert strict_schema(InputOut)["properties"]["category"]["enum"] == list(get_args(Category))


def test_lax_coercion_rejected():
    with pytest.raises(ValidationError):
        OutputOut.model_validate_json('{"reason": "r", "grounded": "true"}')
    with pytest.raises(ValidationError):
        OutputOut.model_validate_json('{"reason": "r", "grounded": 1}')
