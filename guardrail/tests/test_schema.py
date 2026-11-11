"""S1.1 v6 타입: decision_of·to_json·계약 스키마 대조 (master-plan §4.1, ADR-0012)."""

import dataclasses
import json
from typing import get_args

import hypothesis.strategies as st
import jsonschema
import pytest
from hypothesis import given, settings
from strategies import json_values

import guardrail
from guardrail.config import ROOT, load_config
from guardrail.guard import Judgement
from guardrail.schema import (
    MAX_RESPONSE_BYTES,
    Category,
    GuardResponse,
    Message,
    OutputVerdict,
    decision_of,
    parse_request,
)
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


# --- S1.2 parse_request·to_json 상한 (GR-01, GR-09) ---


@pytest.mark.parametrize(
    "body",
    [b"", b"not json", b"[]", b'"x"', b"null", b"{}", b'{"messages": "hi"}', b"\xff\xfe\x00"],
)
def test_parse_request_unreadable_gives_empty_messages(body):
    req = parse_request(body)
    assert req.messages == ()
    assert req.candidate_answer is None and req.has_candidate is False


def test_parse_request_keeps_only_string_content():
    body = json.dumps(
        {
            "messages": [
                {"role": "user", "content": "안녕"},
                {"role": "assistant", "content": 3},
                "문자열 항목",
                {"role": 7, "content": "역할이 숫자"},
                {"content": "역할 없음"},
            ]
        }
    ).encode()
    assert parse_request(body).messages == (
        Message("user", "안녕"),
        Message("", "역할이 숫자"),
        Message("", "역할 없음"),
    )


@pytest.mark.parametrize(
    ("extra", "answer", "has"),
    [
        ({}, None, False),
        ({"candidate_answer": ""}, "", True),
        ({"candidate_answer": "답"}, "답", True),
        ({"candidate_answer": None}, None, True),
        ({"candidate_answer": 3}, None, True),
        ({"candidate_answer": ["a"]}, None, True),
    ],
)
def test_parse_request_candidate_answer(extra, answer, has):
    body = json.dumps({"messages": [{"role": "user", "content": "q"}], **extra}).encode()
    req = parse_request(body)
    assert (req.candidate_answer, req.has_candidate) == (answer, has)


def test_parse_request_candidate_without_messages_list():
    req = parse_request(b'{"messages": 1, "candidate_answer": "a"}')
    assert req.messages == () and req.has_candidate and req.candidate_answer == "a"


def test_parse_request_deep_nesting_does_not_raise():
    assert parse_request(b"[" * 100_000 + b"]" * 100_000).messages == ()


@settings(max_examples=300, deadline=None)
@given(st.binary(max_size=512))
def test_parse_request_never_raises_on_bytes(body):
    parse_request(body)


@settings(max_examples=300, deadline=None)
@given(st.dictionaries(st.sampled_from(["messages", "candidate_answer", "x"]), json_values))
def test_parse_request_never_raises_on_json(obj):
    parse_request(json.dumps(obj).encode())


def test_to_json_drops_unsupported_over_size_cap():
    spans = tuple("가" * 1000 + str(i) for i in range(200))  # 약 600KB
    body = GuardResponse("normal", OutputVerdict(False, spans, True)).to_json(BLOCK)
    assert body["output"]["unsupported"] == []
    assert body["output"]["grounded"] is False
    assert len(json.dumps(body, ensure_ascii=False).encode()) < MAX_RESPONSE_BYTES


def test_to_json_keeps_unsupported_under_cap():
    body = GuardResponse("normal", OutputVerdict(False, ("a",) * 10, True)).to_json(BLOCK)
    assert body["output"]["unsupported"] == ["a"] * 10


@pytest.mark.parametrize("body", [b'{"candidate_answer": "x"', b'\xff"candidate_answer"'])
def test_parse_request_broken_json_with_candidate_key_keeps_output(body):
    req = parse_request(body)
    assert req.messages == () and req.has_candidate and req.candidate_answer is None


def test_to_json_drops_empty_and_unencodable_spans():
    lone = chr(0xD800)  # 짝 없는 surrogate: json.loads는 받지만 UTF-8로 못 씀
    out = OutputVerdict(False, ("", lone, "ok", f"a{lone}b"), True)
    body = GuardResponse("normal", out).to_json(BLOCK)
    assert body["output"]["unsupported"] == ["ok"]
    json.dumps(body, ensure_ascii=False).encode()
