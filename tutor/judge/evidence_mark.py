"""근거 위치 표시(`toggles.mark_evidence`, 측정판). LLM 0회.

주 호출에 보이는 근거 묶음의 검색 1위 섹션에서, 질문과 관련 있어 보이는 단위 둘의 위치만 표시한다.
원문은 자르거나 바꾸지 않고 표시만 더한다(KB에는 `【`가 없어 표시를 걷으면 원문과 같다).
표시한 섹션은 주 호출에만 넘기고, 근거 묶음은 원본을 쓴다(`tutor.judge.draft`).
- 단위: FAQ 짝(`### Q.` 줄과 그 답), 문단, 코드 블록, 표의 행. 머리말 줄과 표 자리 표시(`(표: …)`)는
  단위가 아니다. 표의 머리 행은 그대로 보인다.
- 고르기: 검색 질의와 같은 글(창 안 학습자 발화 + 판단 대상)과 글자 2-gram이 겹치는 수를
  √(단위 2-gram 수)로 나눈 값이 큰 두 단위. 겹침이 없는 단위는 고르지 않는다.
  정답 정보는 쓰지 않는다.
- 표시: 문단·FAQ·코드는 그 단위의 원문 위치 앞뒤에 `OPEN`·`CLOSE` 줄, 표 행은 그 행 끝에 `ROW`.
  같은 글이 섹션 앞쪽에 또 있어도 고른 단위의 자리에 넣는다.
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
_BLOCK = re.compile(r"\S.*?(?=\n[ \t]*\n|\Z)", re.S)  # 빈 줄로 나뉜 블록
_SPACE = re.compile(r"\s+")

# (종류, 단위 글, 자리). 자리는 본문 단위면 ("text", 시작, 끝), 표 행이면 ("row", 표 번호, 줄 번호)
Unit = tuple[str, str, tuple[str, int, int]]


def _bigrams(text: str) -> set[str]:
    s = _SPACE.sub("", text)
    return {s[i : i + 2] for i in range(len(s) - 1)}


def _is_heading(line: str) -> bool:
    """머리말 줄(`#`로 시작). FAQ 질문 줄(`### Q.`)은 머리말로 보지 않는다."""
    s = line.lstrip()
    return s.startswith("#") and not s.lstrip("#").strip().startswith("Q.")


def _prose_units(text: str, base: int) -> list[Unit]:
    """코드 블록 밖 글에서 블록 단위. 블록 앞쪽의 머리말 줄은 빼고, 중간 머리말 앞에서 끊는다."""
    out: list[Unit] = []
    for m in _BLOCK.finditer(text):
        lines = m.group(0).rstrip().split("\n")
        start, i = base + m.start(), 0
        while i < len(lines) and _is_heading(lines[i]):
            start += len(lines[i]) + 1
            i += 1
        body_lines = []
        for line in lines[i:]:
            if _is_heading(line):
                break
            body_lines.append(line)
        body = "\n".join(body_lines).rstrip()
        if not body.strip() or body.lstrip().startswith("(표:"):
            continue
        kind = "faq" if body.lstrip().lstrip("#").strip().startswith("Q.") else "para"
        out.append((kind, body, ("text", start, start + len(body))))
    return out


def units(section: Section) -> list[Unit]:
    """섹션을 표시 단위로 나눈다(코드 블록, 그 밖 본문 블록, 표 행)."""
    text = section.text
    out: list[Unit] = []
    cursor = 0
    for m in _CODE.finditer(text):
        out.extend(_prose_units(text[cursor : m.start()], cursor))
        out.append(("code", m.group(0), ("text", m.start(), m.end())))
        cursor = m.end()
    out.extend(_prose_units(text[cursor:], cursor))
    for index, table in enumerate(section.tables):
        lines = table.split("\n")
        for j, line in enumerate(lines):
            if j > 0 and line.strip():  # 0번 줄은 표 머리
                out.append(("row", line, ("row", index, j)))
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
    text = section.text
    spans = sorted((loc[1], loc[2]) for _, _, loc in picked if loc[0] == "text")
    for start, end in reversed(spans):  # 뒤에서부터 넣어 앞 자리가 밀리지 않게
        text = f"{text[:start]}{OPEN}\n{text[start:end]}\n{CLOSE}{text[end:]}"
    tables = [t.split("\n") for t in section.tables]
    for _, _, loc in picked:
        if loc[0] == "row":
            tables[loc[1]][loc[2]] = f"{tables[loc[1]][loc[2]]} {ROW}"
    return replace(section, text=text, tables=tuple("\n".join(t) for t in tables))
