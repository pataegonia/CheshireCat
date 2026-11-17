"""자료 문장 덧붙이기(`toggles.quote_append`, TU-07). LLM은 가짜다(API 0회).

입력 문장은 지어낸 것이다(공개 세트 문구를 옮기지 않는다).
"""

import asyncio

import pytest

from tests.tutor.test_flow import QUESTION, FakeLLM, out, state_with
from tutor.answer.quote import quote_sentences
from tutor.answer.templates import QUOTE_APPEND_LEAD
from tutor.judge import flow
from tutor.request import parse_request

ON = {"toggles.quote_append": True}


def ask(state, text=QUESTION):
    body = {"messages": [{"role": "user", "content": text}]}
    return asyncio.run(flow.answer_v2(parse_request(body), state, deadline=1e12))


def request(text=QUESTION):
    return parse_request({"messages": [{"role": "user", "content": text}]})


def test_off_keeps_the_answer():
    assert QUOTE_APPEND_LEAD not in ask(state_with(FakeLLM(out())))["answer"]


def test_on_appends_sentences_from_the_first_cited_section():
    state = state_with(FakeLLM(out()), **ON)
    base = ask(state_with(FakeLLM(out())))
    resp = ask(state)
    picked = quote_sentences(state.tutor_kb.sections[resp["citations"][0]].text, QUESTION)
    new = [s for s in picked if s not in base["answer"]]
    assert new  # 이 질문은 PY-01-01 설명 문장과 겹친다
    assert resp["answer"] == f"{base['answer']} {QUOTE_APPEND_LEAD} {' '.join(new)}"
    assert resp["citations"] == base["citations"] and resp["action"] == "answer"


@pytest.mark.parametrize(
    "main",
    [out("answer_seeking", "scaffold"), out("out_of_material", "abstain", sentences=[])],
)
def test_non_answer_actions_are_untouched(main):
    assert QUOTE_APPEND_LEAD not in ask(state_with(FakeLLM(main), **ON))["answer"]


@pytest.mark.parametrize("cites", [[], ["XX-99-99"]], ids=["no-citation", "unknown-section"])
def test_no_citation_or_unknown_section_is_untouched(cites):
    state = state_with(FakeLLM(out()), **ON)
    body = {"answer": "답", "action": "answer", "citations": cites}
    assert flow.append_quote(request(), state, body) is body


def test_sentences_already_in_the_answer_are_not_repeated():
    state = state_with(FakeLLM(out()), **ON)
    picked = quote_sentences(state.tutor_kb.sections["PY-01-01"].text, QUESTION)
    body = {"answer": " ".join(picked), "action": "answer", "citations": ["PY-01-01"]}
    assert flow.append_quote(request(), state, body) is body


def test_over_the_answer_limit_is_untouched():
    state = state_with(FakeLLM(out()), **ON)
    body = {"answer": "가" * 3990, "action": "answer", "citations": ["PY-01-01"]}
    assert flow.append_quote(request(), state, body) is body


def test_input_body_is_not_mutated():
    state = state_with(FakeLLM(out()), **ON)
    body = {"answer": "답", "action": "answer", "citations": ["PY-01-01"]}
    snapshot = dict(body)
    out_body = flow.append_quote(request(), state, body)
    assert body == snapshot and out_body is not body


def test_material_quote_answer_after_main_failure_is_not_doubled():
    # 주 호출 실패 → 자료 인용 답(TU-12)은 이미 같은 고르기의 문장이라 덧붙이지 않는다
    llm = FakeLLM(None, error="timeout")
    state = state_with(llm, **ON, **{"toggles.fallback_quote": True})
    resp = ask(state)
    assert resp["action"] == "answer"
    assert QUOTE_APPEND_LEAD not in resp["answer"]
