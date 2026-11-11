"""S1.1 v6 타입: decision_of·to_json·계약 스키마 대조 (master-plan §4.1, ADR-0012)."""

import dataclasses
import json
from typing import get_args

import jsonschema
import pytest

import guardrail
from guardrail.config import ROOT, load_config
from guardrail.guard import Judgement
from guardrail.schema import Category, GuardResponse, OutputVerdict, decision_of
from guardrail.signals import Signals

CATEGORIES: tuple[str, ...] = get_args(Category)
BLOCK = frozenset({"jailbreak_injection", "harmful_content", "academic_dishonesty", "off_topic"})
CONTRACT = json.loads((ROOT / "docs" / "contract" / "guard.schema.json").read_text("utf-8"))


def test_block_set_matches_default_config():
    assert frozenset(load_config("configs/default.yaml").block_categories) == BLOCK


def test_categories_match_contract():
    assert set(CATEGORIES) == set(CONTRACT["$defs"]["category"]["enum"])
    assert len(CATEGORIES) == 10


@pytest.mark.parametrize("category", CATEGORIES)
def test_decision_of_follows_block_set(category):
    assert decision_of(category, BLOCK) == ("block" if category in BLOCK else "allow")
    assert decision_of(category, frozenset()) == "allow"


def test_to_json_without_output():
    body = GuardResponse(category="off_topic", output=None).to_json(BLOCK)
    assert body == {"input": {"decision": "block", "category": "off_topic"}}


def test_to_json_with_output_copies_category():
    verdict = OutputVerdict(grounded=False, unsupported=("3번",), safe=True)
    body = GuardResponse(category="normal", output=verdict).to_json(BLOCK)
    assert body == {
        "input": {"decision": "allow", "category": "normal"},
        "output": {"grounded": False, "unsupported": ["3번"], "safe": True, "category": "normal"},
    }


@pytest.mark.parametrize("unsupported", [None, (), ("1.3배", "PY-01-02")])
def test_to_json_matches_contract(unsupported):
    out = None if unsupported is None else OutputVerdict(False, unsupported, True)
    body = GuardResponse(category="harmful_content", output=out).to_json(BLOCK)
    schema = {**CONTRACT, "$ref": "#/$defs/response"}
    jsonschema.validate(body, schema)


def test_types_are_frozen():
    sig = Signals(low_signal=False, refusal=False, hidden_tags=False)
    resp = GuardResponse(category="normal", output=None)
    j = Judgement(
        response=resp,
        input_source="default",
        output_tier=None,
        nano_grounded=None,
        signals=sig,
        knn_major=None,
        lr_pred=None,
        calls=0,
        errors=(),
        latency_ms={},
    )
    for obj, field in ((sig, "refusal"), (resp, "category"), (j, "calls")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, field, None)


def test_package_reexports():
    assert guardrail.decision_of is decision_of
    assert {"GuardConfig", "load_config", "GuardResponse"} <= set(guardrail.__all__)
