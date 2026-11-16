"""주 호출 실패 때의 자료 인용 답(TU-12, #201 A-2, `toggles.fallback_quote`)."""

import pytest

from tests.tutor.test_flow import DELEGATION_ASK, QUESTION, FakeLLM, ask, state_with
from tests.tutor.test_guard_merge import FakeGuard, build, run
from tutor.answer.quote import MAX_QUOTED, material_quote, quote_sentences
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


def test_quote_picks_sentences_that_overlap_the_question_in_text_order():
    got = quote_sentences(TEXT_MD, "야구 점수판은 몇 회까지 기록해?")
    assert got[0] == "야구 점수판 예제는 3회까지의 점수를 기록해요."
    assert len(got) <= MAX_QUOTED
    assert got == sorted(got, key=TEXT_MD.replace("**", "").index)  # 본문 순서


def test_quote_skips_headings_questions_answers_and_tables():
    got = quote_sentences(TEXT_MD, "점수판 몇 회까지 기록하나요 3회까지 기록해요 회 점수")
    joined = " ".join(got)
    assert "Q." not in joined and "A." not in joined  # 퀴즈 질문·정답 줄은 옮기지 않는다
    assert "#" not in joined and "|" not in joined


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
    long_loose = (
        "꿈틀 중학교 동아리는 야구와 과일과 성적표와 점수판과 여러 예제를 다루며 "
        "회마다 기록하고 정리하고 다시 확인하는 활동을 오래 해 왔어요."
    )
    exact = "야구 점수판은 3회까지 기록해요."
    got = quote_sentences(f"{long_loose} {exact}", "야구 점수판은 몇 회까지 기록해?")
    assert got[0] == exact or got == [long_loose, exact]
    assert exact in got


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
