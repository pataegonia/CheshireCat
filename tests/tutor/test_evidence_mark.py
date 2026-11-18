"""근거 위치 표시(`toggles.mark_evidence`). LLM은 가짜다(API 0회). 섹션 글은 지어낸 것이다."""

import asyncio
from dataclasses import replace

import pytest

from tests.tutor.test_draft import OUT, TABLE, FakeComplete, Result, u
from tests.tutor.test_flow import QUESTION, FakeLLM, state_with
from tutor.judge import flow
from tutor.judge.conversation import tidy
from tutor.judge.draft import draft_answer
from tutor.judge.evidence_mark import CLOSE, OPEN, ROW, mark, units
from tutor.judge.main_call import MainCallPrompt
from tutor.request import parse_request
from tutor.retrieval import Section
from tutor.retrieval.search import default_index

TEXT = """# XX-01-01 연습 섹션
## 도토리 저금통

도토리 저금통은 매주 월요일에 열어요. 한 번에 다섯 개까지 꺼낼 수 있어요.

```python
acorns = 5
print(acorns)
```

다람쥐 마을 장터는 토요일에만 열려요.

### Q. 저금통을 화요일에 열 수 있나요?
A. 아니요. 저금통은 월요일에만 열어요.

(표: XX-01-01-table1.csv)"""
TABLE_CSV = "항목,내용\n여는 요일,월요일\n꺼내는 개수,다섯 개까지\n"
SECTION = Section(id="XX-01-01", subject="python", title="연습", text=TEXT, tables=(TABLE_CSV,))


def unmark(section: Section) -> Section:
    """표시를 걷어 낸 섹션. `mark`가 원문을 바꾸지 않았는지 보는 데 쓴다."""
    text = section.text.replace(f"{OPEN}\n", "").replace(f"\n{CLOSE}", "")
    tables = tuple(t.replace(f" {ROW}", "") for t in section.tables)
    return replace(section, text=text, tables=tables)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_units_split_faq_paragraph_code_and_table_rows():
    kinds = [kind for kind, _, _ in units(SECTION)]
    assert kinds.count("code") == 1 and kinds.count("faq") == 1 and kinds.count("row") == 2
    bodies = [body for _, body, _ in units(SECTION)]
    assert not any(
        line.startswith("#") and "Q." not in line for b in bodies for line in b.split("\n")
    )
    assert not any(b.startswith("(표:") for b in bodies)
    assert "항목,내용" not in bodies  # 표 머리 행은 단위가 아니다
    for _, body, loc in units(SECTION):  # 본문 단위의 자리는 원문 그 자리다
        if loc[0] == "text":
            assert SECTION.text[loc[1] : loc[2]] == body


def test_mark_picks_two_units_related_to_the_question_without_changing_the_text():
    marked = mark(SECTION, "저금통은 무슨 요일에 열어?", ())
    assert marked is not SECTION and marked.id == SECTION.id
    assert marked.text.count(OPEN) + sum(t.count(ROW) for t in marked.tables) == 2
    assert unmark(marked) == SECTION


def test_mark_marks_a_table_row_at_its_end():
    marked = mark(SECTION, "꺼내는 개수", ())
    assert f"꺼내는 개수,다섯 개까지 {ROW}" in marked.tables[0]
    assert marked.tables[0].startswith("항목,내용\n")


def test_mark_uses_history_turns_too():
    marked = mark(SECTION, "그건?", ("장터는 언제 열려?",))
    assert marked.text.count(f"{OPEN}\n다람쥐 마을 장터") == 1


def test_mark_returns_the_section_unchanged_when_nothing_overlaps():
    assert mark(SECTION, "zzz", ()) is SECTION


def test_mark_puts_the_marker_at_the_picked_unit_even_if_its_text_appears_earlier():
    # 둘째 문단의 글이 첫 문단 안에도 있다. 첫 문단 문장 중간이 아니라 둘째 문단 자리에 넣는다
    text = (
        "저금통은 월요일. 장터와 마을과 도토리와 다람쥐 이야기가 길게 이어져요."
        "\n\n월요일.\n\n화요일에는 닫아요."
    )
    section = Section(id="XX-01-02", subject="python", title="연습", text=text)
    marked = mark(section, "월요일 화요일", ())
    assert marked.text.startswith("저금통은 월요일. 장터와")
    assert f"\n\n{OPEN}\n월요일.\n{CLOSE}\n\n" in marked.text
    assert unmark(marked) == section


def test_mark_and_unmark_round_trip_on_every_kb_section():
    for section in default_index().sections.values():
        marked = mark(section, section.title + " 규칙 예시 표", ())
        assert unmark(marked) == section


@pytest.mark.anyio
@pytest.mark.parametrize("on", [False, True])
async def test_draft_shows_marks_only_to_the_main_call(on):
    complete = FakeComplete(Result(OUT, None))
    d = await draft_answer(
        tidy([u("꿈틀 중학교 동아리 야구 점수판 예제는 몇 회까지 점수를 기록해?")]),
        prompt=MainCallPrompt.load(TABLE),
        complete=complete,
        deadline=1e9,
        k=3,
        mark_top=on,
    )
    sent = complete.messages[0][-1]["content"]
    assert (OPEN in sent) is on
    # 근거 묶음은 켜도 원본이다(자료 인용 답·문장 덧붙이기·인용이 이것을 쓴다)
    assert all(OPEN not in s.text and all(ROW not in t for t in s.tables) for s in d.sections)


def test_fallback_quote_answer_has_no_markers():
    # 주 호출이 실패하면 자료 인용 답이 근거 묶음 1위에서 문장을 고른다. 표시는 주 호출에만 보인다
    state = state_with(
        FakeLLM(None, error="timeout"),
        **{"toggles.fallback_quote": True, "toggles.mark_evidence": True},
    )
    # 질문에 표시 문구와 겹치는 말을 넣어, 표시가 남으면 표시 줄이 문장으로 골라지게 한다
    asked = f"{QUESTION} 질문과 관련 있어 보이는 부분 여기까지"
    req = parse_request({"messages": [{"role": "user", "content": asked}]})
    resp = asyncio.run(flow.answer_v2(req, state, deadline=1e12))
    assert resp["action"] == "answer" and "【" not in resp["answer"]
