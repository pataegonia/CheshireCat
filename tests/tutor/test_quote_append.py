"""자료 문장 덧붙이기(`toggles.quote_append`, 측정용). LLM은 가짜다(API 0회).

입력 문장은 지어낸 것이다(공개 세트 문구를 옮기지 않는다).
"""

import asyncio

import pytest

from tests.tutor.test_flow import QUESTION, FakeLLM, out, state_with
from tutor.answer.quote import quote_sentences
from tutor.judge import flow
from tutor.request import parse_request

ON = {"toggles.quote_append": True}


def ask(state, text=QUESTION):
    body = {"messages": [{"role": "user", "content": text}]}
    return asyncio.run(flow.answer_v2(parse_request(body), state, deadline=1e12))


def test_off_keeps_the_answer():
    off = ask(state_with(FakeLLM(out())))
    assert flow.QUOTE_APPEND_LEAD not in off["answer"]


def test_on_appends_sentences_from_the_first_cited_section():
    state = state_with(FakeLLM(out()), **ON)
    base = ask(state_with(FakeLLM(out())))
    resp = ask(state)
    cited = resp["citations"][0]
    picked = quote_sentences(state.tutor_kb.sections[cited].text, QUESTION)
    assert picked  # 이 질문은 PY-01-01 설명 문장과 겹친다
    assert resp["answer"].startswith(base["answer"])
    assert flow.QUOTE_APPEND_LEAD in resp["answer"]
    assert all(s in resp["answer"] for s in picked if s not in base["answer"])
    assert resp["citations"] == base["citations"] and resp["action"] == "answer"


@pytest.mark.parametrize(
    "main",
    [out("answer_seeking", "scaffold"), out("out_of_material", "abstain", sentences=[])],
)
def test_non_answer_actions_are_untouched(main):
    resp = ask(state_with(FakeLLM(main), **ON))
    assert flow.QUOTE_APPEND_LEAD not in resp["answer"]


def test_no_citation_or_unknown_section_is_untouched():
    state = state_with(FakeLLM(out()), **ON)
    body = {"answer": "답", "action": "answer", "citations": []}
    req = parse_request({"messages": [{"role": "user", "content": QUESTION}]})
    assert flow._append_quote(req, state, body) is body
    body2 = {"answer": "답", "action": "answer", "citations": ["XX-99-99"]}
    assert flow._append_quote(req, state, body2) is body2


def test_sentences_already_in_the_answer_are_not_repeated():
    state = state_with(FakeLLM(out()), **ON)
    req = parse_request({"messages": [{"role": "user", "content": QUESTION}]})
    sec = next(iter(state.tutor_kb.sections))
    picked = quote_sentences(state.tutor_kb.sections["PY-01-01"].text, QUESTION)
    body = {"answer": " ".join(picked), "action": "answer", "citations": ["PY-01-01"]}
    assert sec  # KB가 있다
    assert flow._append_quote(req, state, body) is body


def test_over_the_answer_limit_is_untouched():
    state = state_with(FakeLLM(out()), **ON)
    req = parse_request({"messages": [{"role": "user", "content": QUESTION}]})
    body = {"answer": "가" * 3990, "action": "answer", "citations": ["PY-01-01"]}
    assert flow._append_quote(req, state, body) is body
