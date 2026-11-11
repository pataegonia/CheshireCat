"""응답 계약(TU-01)과 기본 응답(TU-12)."""

import re

import pytest

from tests.tutor.tutor_testlib import REPO, SCHEMA, assert_contract
from tutor.runtime.response import (
    ANSWER_MAX_CHARS,
    CITATION_PATTERN,
    CLARIFY_TEXT,
    TUTOR_DEFAULT,
    default_response,
    ensure_contract,
)


def test_default_is_clarify_with_no_citations_and_valid():
    assert TUTOR_DEFAULT == {"answer": CLARIFY_TEXT, "action": "clarify", "citations": []}
    assert_contract(TUTOR_DEFAULT)


def test_fixed_sentence_has_no_digits():  # 전화번호·번호를 넣지 않는다(TU-12)
    assert not re.search(r"\d", CLARIFY_TEXT)


def test_default_response_is_a_fresh_copy():
    a = default_response()
    a["citations"].append("PY-01-01")
    a["answer"] = "바뀜"
    assert default_response() == TUTOR_DEFAULT
    assert TUTOR_DEFAULT["citations"] == []
    assert TUTOR_DEFAULT["answer"] == CLARIFY_TEXT


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "x",
        [],
        1,
        {},
        {"answer": "a", "action": "answer"},
        {"answer": 1, "action": "answer", "citations": []},
        {"answer": "a", "action": ["answer"], "citations": []},
        {"answer": "a", "action": "answers", "citations": []},
        {"answer": "a", "action": "answer", "citations": None},
        {"answer": "a", "action": "answer", "citations": "PY-01-01"},
    ],
)
def test_ensure_contract_turns_bad_shapes_into_default(bad):
    assert ensure_contract(bad) == TUTOR_DEFAULT


@pytest.mark.parametrize("action", ["ANSWER", " answer", "Answer\n"])
def test_action_spacing_and_case_are_normalized(action):  # 공백 하나로 좋은 답을 버리지 않는다
    out = ensure_contract({"answer": "a", "action": action, "citations": []})
    assert out == {"answer": "a", "action": "answer", "citations": []}


def test_ensure_contract_cuts_answer_at_4000_unicode_chars():
    out = ensure_contract({"answer": "가" * 5000, "action": "answer", "citations": []})
    assert ANSWER_MAX_CHARS == 4000
    assert len(out["answer"]) == 4000
    assert_contract(out)


def test_ensure_contract_keeps_only_spec_fields_and_string_citations():
    out = ensure_contract(
        {"answer": "a", "action": "scaffold", "citations": ["PY-01-01", 3, None], "extra": 1}
    )
    assert out == {"answer": "a", "action": "scaffold", "citations": ["PY-01-01"]}


def test_ensure_contract_makes_lone_surrogates_encodable():
    out = ensure_contract({"answer": "a\ud800b", "action": "answer", "citations": ["\udc00"]})
    out["answer"].encode("utf-8")
    assert out["citations"] == []
    assert_contract(out)


def test_citation_pattern_is_the_schema_pattern():
    assert CITATION_PATTERN == SCHEMA["properties"]["citations"]["items"]["pattern"]


def test_ensure_contract_drops_citations_that_are_not_kb_section_ids():
    wrong = ["FIN-01", "PY-01-01-table1", "PY-01-01.md", "py-01-01", "PY-01-01\n", "질문 내용"]
    out = ensure_contract({"answer": "a", "action": "answer", "citations": ["PY-01-01", *wrong]})
    assert out["citations"] == ["PY-01-01"]


def test_schema_citation_pattern_matches_every_kb_section_only():
    pattern = re.compile(SCHEMA["properties"]["citations"]["items"]["pattern"])
    ids = [p.stem for p in (REPO / "competition" / "kb").rglob("*.md")]
    assert len(ids) == 48
    assert all(pattern.search(i) for i in ids)
    for wrong in ("FIN-01", "PY-01-01-table1", "PY-01-01.md", "py-01-01"):
        assert not pattern.search(wrong)
