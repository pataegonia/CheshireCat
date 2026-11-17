"""KB에 없는 이름 붙은 말 힌트(`toggles.unknown_term_hint`). LLM은 가짜다(API 0회).

입력 문장은 지어낸 것이다(공개 세트 문구를 옮기지 않는다).
"""

import asyncio

import pytest

from tests.tutor.test_flow import FakeLLM, out, state_with
from tutor.judge import flow, unknown_terms
from tutor.request import parse_request

BLOB = unknown_terms.squash("적금의 기본 이자율과 납입 한도, 반복문 규칙을 다룬다")


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("이 과정의 별빛 등급 규칙은 뭐예요?", ["별빛등급"]),  # KB에 없는 이름
        ("납입 한도는 얼마예요?", []),  # KB에 있는 이름
        ("반복문 규칙 알려 줘", []),
        ("누군지 규칙 없이 말해 줘", []),  # 용언 꼴 앞말은 이름이 아니다
        ("어떤 기준으로 골라요?", []),  # 관형·지시어 앞말
        ("달빛 지수와 달빛 지수", ["달빛지수"]),  # 중복 없음
        ("구름의 한도", ["구름한도"]),  # 앞말 조사 떼기
    ],
)
def test_missing_terms(text, want):
    assert unknown_terms.missing_terms(text, BLOB) == want


def test_hint_text():
    assert unknown_terms.hint([]) is None
    assert unknown_terms.hint(["별빛등급"]).startswith("자료에 없는 이름: 별빛등급")


def _ask(state, text):
    body = {"messages": [{"role": "user", "content": text}]}
    return asyncio.run(flow.answer_v2(parse_request(body), state, deadline=1e12))


@pytest.mark.parametrize("on", [True, False])
def test_hint_goes_into_the_main_call_only_when_on(on):
    llm = FakeLLM(out())
    state = state_with(llm, **{"toggles.unknown_term_hint": on})
    assert (state.tutor_kb_blob is not None) is on
    _ask(state, "이 과정의 별빛 등급 규칙은 뭐예요?")
    user = llm.calls[0]["messages"][-1]["content"]
    assert ("자료에 없는 이름: 별빛등급" in user) is on


def test_real_kb_names_are_not_flagged():
    llm = FakeLLM(out())
    state = state_with(llm, **{"toggles.unknown_term_hint": True})
    blob = state.tutor_kb_blob
    # KB에 실제로 있는 말은 걸리지 않는다(섹션 제목에서 뽑은 낱말로 확인)
    title = next(iter(state.tutor_kb.sections.values())).title
    assert unknown_terms.squash(title) in blob
