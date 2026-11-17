"""KB에 없는 이름 붙은 말 찾기(`toggles.oom_gate: named_term`). API 0회.

입력 문장은 지어낸 것이다(공개 세트 문구를 옮기지 않는다).
"""

import pytest

from tests.tutor.test_flow import FakeLLM, out, state_with
from tutor.judge import unknown_terms

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
        ("변수가 뭐예요?", []),  # 머리말이 없다
    ],
)
def test_missing_terms(text, want):
    assert unknown_terms.missing_terms(text, BLOB) == want


def test_squash_keeps_only_letters_and_digits():
    assert unknown_terms.squash("A-1 등급 (규칙)!") == "a1등급규칙"


def test_real_kb_titles_are_in_the_kb_text():
    state = state_with(FakeLLM(out()), **{"toggles.oom_gate": "named_term"})
    blob = state.tutor_kb_blob
    assert blob  # 기동 때 한 번 만든다
    for section in list(state.tutor_kb.sections.values())[:20]:
        assert unknown_terms.squash(section.text)[:30] in blob
