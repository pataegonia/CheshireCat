"""I1.2 계약 타입(PRD IN-01): 스키마 round-trip, action 7종, citations 검증."""

from pathlib import Path
from typing import get_args

import guardrail.schema
import pytest
from pydantic import ValidationError

from integration import contracts
from integration.contracts import (
    ACTIONS,
    SECTION_IDS,
    Action,
    Evidence,
    Section,
    Sentence,
    TutorResult,
)

ROOT = Path(__file__).resolve().parents[2]


def test_action_is_the_seven_spec_values():
    expected = ("answer", "scaffold", "clarify", "abstain", "redirect", "refuse", "escalate")
    assert get_args(Action) == expected
    assert ACTIONS == expected


def test_category_is_imported_from_guard_not_redefined():
    assert contracts.Category is guardrail.schema.Category


def test_section_ids_match_the_kb_files():
    on_disk = {p.stem for p in (ROOT / "competition" / "kb").glob("*/*.md")}
    assert len(SECTION_IDS) == 48
    assert SECTION_IDS == on_disk


@pytest.mark.parametrize(
    "model",
    [
        Section(
            id="PY-01-01",
            subject="python",
            title="변수",
            text="본문",
            tables=["a,b\n1,2"],
            names=["꿈틀 중학교"],
        ),
        Evidence(sid="PR-02-03", quote="역할을 적는다"),
        Sentence(text="일반 설명이에요.", kind="non_claim"),
        Sentence(
            text="3회까지 기록해요.",
            kind="claim",
            evidence=[Evidence(sid="PY-01-01", quote="3회까지")],
        ),
        TutorResult(answer="3회까지 기록해요.", action="answer", citations=["PY-01-01"]),
        TutorResult(answer="", action="refuse", citations=[]),
    ],
)
def test_round_trip(model):
    cls = type(model)
    assert cls.model_validate_json(model.model_dump_json()) == model
    assert cls.model_validate(model.model_dump()) == model


def test_section_optional_lists_default_empty():
    s = Section(id="FIN-04-04", subject="finance", title="t", text="x")
    assert s.tables == [] and s.names == []


def test_sentence_evidence_defaults_empty_and_is_not_shared():
    a = Sentence(text="a", kind="claim")
    b = Sentence(text="b", kind="claim")
    a.evidence.append(Evidence(sid="PY-01-01", quote="q"))
    assert b.evidence == []


def test_tutor_result_dumps_exactly_the_response_keys():
    r = TutorResult(answer="a", action="answer", citations=["FIN-01-02"])
    assert r.model_dump() == {"answer": "a", "action": "answer", "citations": ["FIN-01-02"]}


@pytest.mark.parametrize("sid", sorted(["PY-01-01", "PR-04-04", "FIN-02-03"]))
def test_citations_accept_real_sections(sid):
    assert TutorResult(answer="a", action="answer", citations=[sid]).citations == [sid]


@pytest.mark.parametrize(
    "bad",
    [
        "py-01-01",  # 소문자
        "PY-1-01",  # 자릿수
        "XX-01-01",  # 없는 과목
        "PY-01-01 ",  # 뒤 공백
        "PY-01-01\n",  # 뒤 줄바꿈($가 통과시키는 경우)
        "PY-01-01-table1",  # 표 파일 이름
        "PY-09-01",  # 정규식은 맞지만 실존하지 않음
        "FIN-01-00",  # 정규식은 맞지만 실존하지 않음
        "",
    ],
)
def test_citations_reject(bad):
    with pytest.raises(ValidationError):
        TutorResult(answer="a", action="answer", citations=["PY-01-01", bad])


def test_citations_reject_non_string():
    with pytest.raises(ValidationError):
        TutorResult(answer="a", action="answer", citations=[101])


def test_unknown_action_rejected():
    with pytest.raises(ValidationError):
        TutorResult(answer="a", action="allow", citations=[])


def test_unknown_sentence_kind_rejected():
    with pytest.raises(ValidationError):
        Sentence(text="a", kind="fact")
