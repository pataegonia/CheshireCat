"""근거 위치 표시(`toggles.mark_evidence`). LLM은 가짜다(API 0회). 섹션 글은 지어낸 것이다."""

import pytest

from tests.tutor.test_draft import OUT, TABLE, FakeComplete, Result, u
from tutor.judge.conversation import tidy
from tutor.judge.draft import draft_answer
from tutor.judge.evidence_mark import CLOSE, OPEN, ROW, mark, units
from tutor.judge.main_call import MainCallPrompt
from tutor.retrieval import Section

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


@pytest.fixture
def anyio_backend():
    return "asyncio"


def strip_marks(section: Section) -> tuple[str, tuple[str, ...]]:
    text = section.text.replace(f"{OPEN}\n", "").replace(f"\n{CLOSE}", "")
    return text, tuple(t.replace(f" {ROW}", "") for t in section.tables)


def test_units_split_faq_paragraph_code_and_table_rows():
    kinds = [kind for kind, _, _ in units(SECTION)]
    assert kinds.count("code") == 1 and kinds.count("faq") == 1 and kinds.count("row") == 2
    bodies = [body for _, body, _ in units(SECTION)]
    assert not any(b.startswith("#") and "Q." not in b for b in bodies)  # 머리말 줄은 단위가 아니다
    assert not any(b.startswith("(표:") for b in bodies)
    assert "항목,내용" not in bodies  # 표 머리 행은 단위가 아니다


def test_mark_picks_two_units_related_to_the_question_without_changing_the_text():
    marked = mark(SECTION, "저금통은 무슨 요일에 열어?", ())
    assert marked is not SECTION and marked.id == SECTION.id
    assert marked.text.count(OPEN) + sum(t.count(ROW) for t in marked.tables) == 2
    assert strip_marks(marked) == (SECTION.text, SECTION.tables)
    assert (
        f"{OPEN}\n### Q. 저금통을 화요일에" in marked.text
        or f"{OPEN}\n도토리 저금통은" in marked.text
    )


def test_mark_marks_a_table_row_at_its_end():
    marked = mark(SECTION, "꺼내는 개수", ())
    assert f"꺼내는 개수,다섯 개까지 {ROW}" in marked.tables[0]
    assert marked.tables[0].startswith("항목,내용\n")


def test_mark_uses_history_turns_too():
    assert (
        mark(SECTION, "그건?", ("장터는 언제 열려?",)).text.count(f"{OPEN}\n다람쥐 마을 장터") == 1
    )


def test_mark_returns_the_section_unchanged_when_nothing_overlaps():
    assert mark(SECTION, "zzz", ()) is SECTION


@pytest.mark.anyio
@pytest.mark.parametrize("on", [False, True])
async def test_draft_marks_only_the_top_section_when_on(on):
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
    assert (OPEN in d.sections[0].text) is on
    assert all(OPEN not in s.text and all(ROW not in t for t in s.tables) for s in d.sections[1:])
