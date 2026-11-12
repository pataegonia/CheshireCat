"""고정·템플릿 문장."""

import re

from tutor.answer.templates import ACTION, EMPTY_ANSWER, TEXT, Fixed, empty_answer, fixed_body

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


def test_empty_redirect_follows_category():
    # 리뷰 지적: 비하 발언(harmful_content → redirect)에도 "수업과 관련이 없어요"가 나갔다
    assert empty_answer("redirect", "harmful_content") == TEXT[Fixed.REDIRECT_HARMFUL]
    assert empty_answer("redirect", "off_topic") == TEXT[Fixed.REDIRECT_OFF_TOPIC]
    assert empty_answer("redirect") == TEXT[Fixed.REDIRECT_OFF_TOPIC]
    for action in ACTIONS:  # 범주를 몰라도, 다른 범주여도 action별 문장
        assert empty_answer(action) == EMPTY_ANSWER[action]
        assert empty_answer(action, "normal") == EMPTY_ANSWER[action]


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


def test_template_for_covers_every_template_rule_in_table():
    from integration.actions import load_actions
    from tutor.answer.templates import template_for

    for category, rule in load_actions().items():
        for action in rule.actions:
            kind = template_for(category, action)
            if rule.generate == "template":
                assert kind is not None, (category, action)
                assert ACTION[kind] == action
            else:
                assert kind is None, (category, action)
