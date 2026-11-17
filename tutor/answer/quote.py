"""자료 인용 답(TU-12, #201 A-2, `toggles.fallback_quote`). LLM을 부르지 않는다.

주 호출이 실패했을 때 검색 1위 섹션에서 질문과 가장 많이 겹치는 문장을 그대로 옮기고 그 섹션을
인용한다. 지금 기본값(clarify)은 정답이 answer인 normal 문항에서 action·인용·사실을 모두 잃는다.

- 문장은 섹션 md 본문의 설명 문단에서 고른다. 제목(`#`)·질문(`Q.`)·답(`A.`)·표·인용·구분선 줄,
  코드 블록 안, 표 파일 안내 줄, 굵게만 쓴 이름표 줄은 쓰지 않는다. `A.` 줄은 퀴즈 정답이라
  정답만 요구하는 질문에 새지 않게 뺀다(#201 제안서 3-2 E의 정답 줄).
- 점수는 정규화한 문자 2-gram 겹침을 문장 길이로 나눈 값이다(겹친 종류 수 / √문장 2-gram 수,
  코사인과 같은 순서). 긴 문장이 겹침 수만으로 이기지 않게 한다. 같으면 앞 문장.
- 질문과 겹치는 2-gram 종류가 `MIN_SHARED`개보다 적은 문장은 옮기지 않는다. 한 글자 짝이 우연히
  겹친 무관한 섹션 문장으로 answer를 내지 않게 한다(11/17 리뷰. 공개·점검 세트에서 모든 주 호출이
  실패했다고 두면 normal 101행 중 1행만 겹침 1개였다).
- 옮길 문장이 없으면 None이다. 부르는 쪽이 지금 기본값을 낸다.
- 요청 내용은 지역 변수로만 쓴다(TU-18).
"""

import math
import re
from collections.abc import Sequence
from typing import Any

from tutor.answer.templates import MATERIAL_QUOTE_LEAD
from tutor.retrieval import Section
from tutor.retrieval.kb import bigrams

MAX_QUOTED = 2  # 옮기는 자료 문장 수. 첫 문장까지 3문장 이내(TU-06 1–3문장)
MAX_SENTENCE_CHARS = 400  # 이보다 긴 문장은 옮기지 않는다(답변 4,000자 안, 한 줄 문단 대비)
MIN_SHARED = 2  # 질문과 겹치는 2-gram 종류가 이보다 적은 문장은 옮기지 않는다
_FENCE = re.compile(r"^\s*(```|~~~)")
_SKIP_LINE = re.compile(r"^\s*(#|Q\.|A\.|\||>|-{3,}\s*$|\(표:)")
_LABEL_ONLY = re.compile(r"^\s*\*\*[^*]+\*\*\s*:?\s*$|^[^.!?]{0,30}:\s*$")
_LIST_MARK = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _prose_lines(text: str) -> list[str]:
    """설명 문단 줄만 남긴다(코드 블록 안·건너뛸 줄·이름표 줄은 버린다)."""
    out: list[str] = []
    in_code = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_code = not in_code
            continue
        if in_code or not line.strip() or _SKIP_LINE.match(line) or _LABEL_ONLY.match(line):
            continue
        out.append(line)
    return out


def _sentences(text: str) -> list[str]:
    """설명 문단을 문장으로 나눈다. 목록 기호·굵게·코드 표시(`)는 지운다."""
    out: list[str] = []
    for line in _prose_lines(text):
        plain = _LIST_MARK.sub("", line).replace("**", "").replace("`", "").strip()
        out.extend(
            s.strip()
            for s in _SENTENCE_END.split(plain)
            if s.strip() and len(s.strip()) <= MAX_SENTENCE_CHARS
        )
    return out


def _score(asked: set[str], sentence: str) -> float:
    """겹친 종류 수 / √문장 2-gram 수. 겹친 종류가 `MIN_SHARED`개보다 적으면 0."""
    grams = set(bigrams(sentence))
    shared = len(asked & grams)
    if not grams or shared < MIN_SHARED:
        return 0.0
    return shared / math.sqrt(len(grams))


def quote_sentences(text: str, query: str) -> list[str]:
    """질문과 겹치는 문장을 점수 순으로 `MAX_QUOTED`개까지, 본문 순서대로 돌려준다.

    둘째부터는 가장 높은 점수의 절반 이상일 때만 넣는다(관련 없는 문장을 덧붙이지 않게).
    """
    asked = set(bigrams(query))
    scored = [(_score(asked, s), -i, s) for i, s in enumerate(_sentences(text))]
    ranked = sorted((t for t in scored if t[0] > 0), reverse=True)[:MAX_QUOTED]
    if not ranked:
        return []
    top = ranked[0][0]
    kept = [t for t in ranked if t[0] * 2 >= top]
    return [s for _score_, _neg, s in sorted(kept, key=lambda t: -t[1])]


def material_quote(sections: Sequence[Section], query: str) -> dict[str, Any] | None:
    """검색 1위 섹션의 자료 인용 답(answer + 그 섹션 인용). 쓸 문장이 없으면 None."""
    if not sections:
        return None
    top = sections[0]
    quoted = quote_sentences(top.text, query)
    if not quoted:
        return None
    answer = " ".join([MATERIAL_QUOTE_LEAD, *quoted])
    return {"answer": answer, "action": "answer", "citations": [top.id]}
