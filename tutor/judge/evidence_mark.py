"""근거 위치 표시(`toggles.mark_evidence`, 측정판). LLM 0회.

주 호출에 보이는 근거 묶음의 검색 1위 섹션에서, 질문과 관련 있어 보이는 단위 둘의 위치만 표시한다.
원문은 자르거나 바꾸지 않고 표시 줄만 더한다.
- 단위: FAQ 짝(`### Q.` 줄과 그 답), 문단, 코드 블록, 표의 행. 머리말 줄과 표 자리 표시(`(표: …)`)는
  단위가 아니다. 표의 머리 행은 그대로 보인다.
- 고르기: 검색 질의와 같은 글(창 안 학습자 발화 + 판단 대상)과 글자 2-gram이 겹치는 수를
  √(단위 2-gram 수)로 나눈 값이 큰 두 단위. 겹침이 없는 단위는 고르지 않는다.
  정답 정보는 쓰지 않는다.
- 표시: 문단·FAQ·코드는 앞뒤에 `OPEN`·`CLOSE` 줄, 표 행은 행 끝에 `ROW`.
- 요청 내용은 지역 변수로만 쓴다(TU-18).
"""

import re
from collections.abc import Sequence
from dataclasses import replace

from tutor.retrieval import Section

OPEN = "【질문과 관련 있어 보이는 부분】"
CLOSE = "【여기까지】"
ROW = "←【질문과 관련 있어 보이는 행】"
PICK = 2  # 표시하는 단위 수
_CODE = re.compile(r"```.*?```", re.S)
_BLANK = re.compile(r"\n\s*\n")
_SPACE = re.compile(r"\s+")

# (종류, 단위 글, 표 번호 또는 None)
Unit = tuple[str, str, int | None]


def _bigrams(text: str) -> set[str]:
    s = _SPACE.sub("", text)
    return {s[i : i + 2] for i in range(len(s) - 1)}


def _is_heading(line: str) -> bool:
    """머리말 줄(`#`로 시작). FAQ 질문 줄(`### Q.`)은 머리말로 보지 않는다."""
    s = line.lstrip()
    return s.startswith("#") and not s.lstrip("#").strip().startswith("Q.")


def units(section: Section) -> list[Unit]:
    """섹션을 표시 단위로 나눈다(나온 순서: 코드 블록, 본문 블록, 표 행)."""
    out: list[Unit] = [("code", m.group(0), None) for m in _CODE.finditer(section.text)]
    for block in _BLANK.split(_CODE.sub("\n\n", section.text)):
        block = block.strip()
        if not block or block.startswith("(표:"):
            continue
        block = "\n".join(ln for ln in block.splitlines() if not _is_heading(ln)).strip()
        if not block:
            continue
        out.append(("faq" if "Q." in block.splitlines()[0] else "para", block, None))
    for index, table in enumerate(section.tables):
        rows = [line for line in table.splitlines() if line.strip()]
        out.extend(("row", row, index) for row in rows[1:])
    return out


def mark(section: Section, query: str, history: Sequence[str]) -> Section:
    """질문과 관련 있어 보이는 단위 둘에 표시를 넣은 섹션. 고를 단위가 없으면 그대로 돌려준다."""
    asked = _bigrams("\n".join([*history, query]))
    scored = []
    for unit in units(section):
        grams = _bigrams(unit[1])
        if grams:
            scored.append((len(asked & grams) / len(grams) ** 0.5, unit))
    picked = [unit for score, unit in sorted(scored, key=lambda x: -x[0])[:PICK] if score > 0]
    if not picked:
        return section
    text, tables = section.text, list(section.tables)
    for kind, body, table in picked:
        if kind == "row" and table is not None:
            tables[table] = tables[table].replace(body, f"{body} {ROW}", 1)
        else:
            text = text.replace(body, f"{OPEN}\n{body}\n{CLOSE}", 1)
    return replace(section, text=text, tables=tuple(tables))
