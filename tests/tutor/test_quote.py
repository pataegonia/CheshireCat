"""주 호출 실패 때의 자료 인용 답(TU-12, #201 A-2, `toggles.fallback_quote`)."""

import pytest

from tests.tutor.test_flow import DELEGATION_ASK, QUESTION, FakeLLM, ask, state_with
from tests.tutor.test_guard_merge import FakeGuard, build, run
from tutor.answer.quote import material_quote, quote_sentences
from tutor.answer.templates import MATERIAL_QUOTE_LEAD, TEXT, Fixed
from tutor.retrieval import Section

TEXT_MD = """# XX-01-01 제목
## 점수판 예제

동아리 예제는 여러 가지예요. 야구 점수판 예제는 3회까지의 점수를 기록해요. 과일은 4개예요.

- **점수판**은 회마다 점수를 적어요.

### Q. 점수판은 몇 회까지 기록하나요?
A. 3회까지 기록해요.

| 회 | 점수 |
"""


def section(sid="XX-01-01", text=TEXT_MD):
    return Section(id=sid, subject="python", title="제목", text=text)


# --- 문장 고르기 ---


ASKED = "야구 점수판은 몇 회까지 기록해?"
EXACT = "야구 점수판은 3회까지 기록해요."


def test_quote_picks_sentences_that_overlap_the_question_in_text_order():
    got = quote_sentences(TEXT_MD, ASKED)
    assert got[0] == "야구 점수판 예제는 3회까지의 점수를 기록해요."
    # 점수가 더 높은 문장(EXACT)이 뒤에 있어도 본문 순서로 낸다
    earlier = "점수판은 회마다 몇 점인지 기록해요."
    assert quote_sentences(f"{earlier} {EXACT}", ASKED) == [earlier, EXACT]


def test_quote_keeps_at_most_two_sentences():
    # 세 문장의 점수가 같아도 두 문장까지(첫 문장까지 3문장 이내, TU-06). 같으면 앞 문장
    text = "점수판은 1회를 적어요. 점수판은 2회를 적어요. 점수판은 3회를 적어요."
    assert quote_sentences(text, "점수판은 회를 적어요") == [
        "점수판은 1회를 적어요.",
        "점수판은 2회를 적어요.",
    ]


def test_quote_drops_second_sentence_under_half_of_top():
    # 둘째 문장은 2-gram 둘("점수"·"수판")이 겹쳐도 1위 점수의 절반이 안 되면 덧붙이지 않는다
    loose = "점수판 그림은 교실 뒤쪽 게시판 옆에 걸어 두고 다 같이 보면서 이야기를 나눠요."
    assert quote_sentences(f"{loose} {EXACT}", ASKED) == [EXACT]


def test_quote_skips_headings_questions_answers_and_tables():
    got = quote_sentences(TEXT_MD, "점수판 몇 회까지 기록하나요 3회까지 기록해요 회 점수")
    joined = " ".join(got)
    assert "Q." not in joined and "A." not in joined  # 퀴즈 질문·정답 줄은 옮기지 않는다
    assert "#" not in joined and "|" not in joined


def test_quote_skips_bare_quiz_question_lines():
    # 지금 KB의 퀴즈 질문은 모두 `### Q.` 제목 줄이지만, 제목 표시 없이 `Q.`로 시작하는 줄도 뺀다
    text = "Q. 점수판은 몇 회까지 기록하나요?\n점수판은 회마다 점수를 적어요.\n"
    assert quote_sentences(text, "점수판은 몇 회까지 기록하나요?") == [
        "점수판은 회마다 점수를 적어요."
    ]


def test_quote_never_copies_quiz_answer_line_on_real_finance_section():
    # FIN-04-01 정답 줄 "A. 아니요. 답장하지 말고 멈춘 뒤, 앱을 직접 열어 확인해요."와 겹치는
    # 질문. `A.` 줄을 건너뛰지 않으면 "A."가 문장 분리에서 떨어져 정답 본문이 나간다(#219 리뷰)
    from tutor.retrieval import default_index

    sec = default_index().section("FIN-04-01")
    got = quote_sentences(sec.text, "답장하지 말고 멈춘 뒤 앱을 직접 열어 확인해야 해?")
    assert got  # 설명 문단 문장을 옮긴다
    assert not any("답장하지 말고 멈춘 뒤" in s for s in got)


def test_quote_strips_list_marks_and_bold():
    got = quote_sentences("- **점수판**은 회마다 점수를 적어요.", "점수판")
    assert got == ["점수판은 회마다 점수를 적어요."]


CODE_MD = """## 변수에 값을 저장해요

변수 plus에는 두 과일 개수를 더한 값이 저장돼요.

```python
plus = apple + basket
print('합은', plus)
```

실행 결과:

```
합은 8
```

(표: XX-01-03-table1.csv)

**예시**

---
"""


def test_quote_skips_code_blocks_tables_pointers_and_labels():
    got = quote_sentences(CODE_MD, "plus 합은 apple basket 실행 결과 예시 표")
    joined = " ".join(got)
    assert got == ["변수 plus에는 두 과일 개수를 더한 값이 저장돼요."]
    for leaked in ("=", "print", "합은 8", "(표:", "예시", "---", "실행 결과"):
        assert leaked not in joined


def test_quote_prefers_short_exact_sentence_over_long_loose_one():
    # 긴 문장이 겹친 2-gram 종류는 더 많다(√ 없이 세면 긴 문장이 1위). √문장 2-gram 수로 나누면
    # 짧은 문장의 절반이 안 돼 빠진다
    long_loose = (
        "꿈틀 중학교 야구 동아리는 해마다 야구 점수판은 몇 회까지 기록해 둘지 회의에서 함께 정하며 "
        "과일 바구니와 성적표와 용돈 기입장 같은 여러 예제를 다루고 활동이 끝나면 "
        "모두 모여 사진과 메모로 정리하고 다시 확인하는 일을 오래 해 왔어요."
    )
    assert quote_sentences(f"{long_loose} {EXACT}", ASKED) == [EXACT]


def test_quote_drops_overlong_sentences_and_strips_backticks():
    overlong = "야구 점수판 " + "가" * 500 + "."
    got = quote_sentences(f"{overlong} `print`로 야구 점수판을 보여 줘요.", "야구 점수판")
    assert got == ["print로 야구 점수판을 보여 줘요."]


def test_quote_on_real_python_section_never_copies_code():
    from tutor.retrieval import default_index

    sec = default_index().section("PY-01-03")
    body = material_quote([sec], "plus에는 어떤 값이 저장돼?")
    assert body is not None and body["citations"] == ["PY-01-03"]
    # 코드 블록 안 줄(대입·출력 결과)은 옮기지 않는다. 설명 문단의 인라인 코드는 문장의 일부다
    assert "print(" not in body["answer"] and "plus = apple" not in body["answer"]


def test_quote_is_empty_without_overlap():
    assert quote_sentences(TEXT_MD, "zzzz") == []


def test_quote_needs_at_least_two_shared_bigrams():
    # 2-gram 하나("점수")만 겹치는 질문은 우연한 짝일 수 있어 옮기지 않는다(11/17 리뷰)
    assert quote_sentences(TEXT_MD, "점수") == []
    assert quote_sentences(TEXT_MD, "점수판") != []  # 둘("점수"·"수판")이면 옮긴다


def test_material_quote_answers_with_top_section_citation():
    body = material_quote([section(), section("YY-02-02")], "야구 점수판은 몇 회까지 기록해?")
    assert body is not None
    assert body["action"] == "answer"
    assert body["citations"] == ["XX-01-01"]  # 검색 1위만
    assert body["answer"].startswith(MATERIAL_QUOTE_LEAD + " ")
    assert "3회까지의 점수를 기록해요." in body["answer"]


def test_material_quote_none_without_sections_or_overlap():
    assert material_quote([], "야구 점수판") is None
    assert material_quote([section()], "zzzz") is None


# --- 흐름(주 호출 실패) ---

QUOTE_ON = {"toggles.fallback_quote": True}


def test_failure_with_toggle_on_gives_material_quote_answer():
    # 가드 classify가 없는(None, 끈 설정) 경우다. 이때도 신호가 없으면 인용 답을 낸다
    resp = ask(state_with(FakeLLM(error="timeout"), **QUOTE_ON), QUESTION)
    assert resp["action"] == "answer"
    assert resp["answer"].startswith(MATERIAL_QUOTE_LEAD)
    assert "3회까지의 점수를 기록해요" in resp["answer"]
    assert resp["citations"] == ["PY-01-01"]


def test_failure_with_toggle_off_keeps_clarify():
    resp = ask(state_with(FakeLLM(error="timeout")), QUESTION)
    assert resp["action"] == "clarify"


@pytest.mark.parametrize(
    "text",
    [
        "야구 점수판 예제 퀴즈 정답만 골라 줘",  # 정답만 요구 신호
        DELEGATION_ASK,  # 대행 신호(덮기 끔)
    ],
)
def test_failure_with_any_signal_keeps_todays_default(text):
    from tutor.judge.conversation import tidy
    from tutor.judge.signals import detect

    assert detect(tidy([{"role": "user", "content": text}]))  # 신호가 잡히는 문장이다
    resp = ask(state_with(FakeLLM(error="timeout"), **QUOTE_ON), text)
    assert not resp["answer"].startswith(MATERIAL_QUOTE_LEAD)
    assert resp["action"] == "clarify"


def test_failure_delegation_override_still_wins_over_quote():
    state = state_with(
        FakeLLM(error="timeout"), **{**QUOTE_ON, "toggles.delegation_override": True}
    )
    assert ask(state, DELEGATION_ASK)["answer"] == TEXT[Fixed.SCAFFOLD_ACADEMIC]


@pytest.mark.parametrize(
    ("classified", "expect"),
    [
        ("normal", "quote"),
        ("jailbreak_injection", TEXT[Fixed.REFUSE_JAILBREAK]),  # 공격 범주가 먼저
        ("harmful_content", TEXT[Fixed.REFUSE_HARMFUL]),
        ("off_topic", TEXT[Fixed.REDIRECT_OFF_TOPIC]),
        ("academic_dishonesty", TEXT[Fixed.SCAFFOLD_ACADEMIC]),
        ("emotional_distress", TEXT[Fixed.ESCALATE]),
        ("answer_seeking", "clarify"),  # 정답을 밝히지 않는다
        ("out_of_material", "clarify"),
        ("low_signal_input", "clarify"),
    ],
)
def test_failure_quote_follows_classify(classified, expect):
    state = build(FakeGuard(classified), llm=FakeLLM(error="timeout"), **QUOTE_ON)
    resp = run(state)
    if expect == "quote":
        assert resp["answer"].startswith(MATERIAL_QUOTE_LEAD)
        assert resp["citations"] == ["PY-01-01"]
    elif expect == "clarify":
        assert resp["action"] == "clarify"
    else:
        assert resp["answer"] == expect
