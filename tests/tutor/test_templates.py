"""고정·템플릿 문장."""

import re

from tutor.answer.templates import ACTION, EMPTY_ANSWER, TEXT, Fixed, fixed_body

ACTIONS = {"answer", "scaffold", "clarify", "abstain", "redirect", "refuse", "escalate"}


def test_every_fixed_kind_has_text_and_valid_action():
    for kind in Fixed:
        assert TEXT[kind].strip()
        assert ACTION[kind] in ACTIONS


def test_fixed_body_is_spec_shaped():
    for kind in Fixed:
        body = fixed_body(kind)
        assert set(body) == {"answer", "action", "citations"}
        assert body["citations"] == []
        assert len(body["answer"]) <= 4000


def test_empty_answer_covers_all_actions():
    assert set(EMPTY_ANSWER) == ACTIONS
    assert all(t.strip() for t in EMPTY_ANSWER.values())


def test_no_phone_numbers_agency_names_or_grading_talk():
    texts = [*TEXT.values(), *EMPTY_ANSWER.values()]
    for t in texts:
        assert not re.search(r"\d", t), t  # 전화번호·수치 없음
        assert not re.search(r"(센터|치유원|복지|1388|109|채점|점수|평가자)", t), (
            t
        )  # TD-08, SPEC §6


def test_no_subject_facts_or_section_ids():
    for t in [*TEXT.values(), *EMPTY_ANSWER.values()]:
        assert not re.search(r"(PY|PR|FIN)-\d", t)
        for word in ["다람쥐", "꿈틀", "Helpy", "파이썬", "적금"]:
            assert word not in t


def test_answers_are_short():
    # 정답 답변 중앙값 12–108자(PRD §1). 판정 모델이 처리하기 쉬운 짧은 문장
    for t in TEXT.values():
        assert len(t) <= 200, t
