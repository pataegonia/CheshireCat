"""단원 닻(TU-03, `toggles.anchor_unit`, #201 A-6 U). 기본 끔.

여러 턴 대화의 정답 섹션은 대개 앞 대화가 다루던 단원에 있다. 닻 = 마지막 학습자 발화를 뺀
앞 대화(학습자 + 튜터 발화 끝 몇 개)만으로 검색한 1위 섹션의 단원이다. 본 검색 상위 k에서 닻
단원 섹션을 앞으로 옮긴다(순서만 바꾸고 섹션 집합은 그대로).

닻은 이번 요청의 발화로만 정하고 남기지 않는다(TU-18). LLM·임베딩 호출 없이 기동 때 만든 BM25
색인만 쓴다.
"""

from collections.abc import Sequence

from tutor.retrieval.index import KbIndex
from tutor.retrieval.kb import Section
from tutor.retrieval.search import retrieve


def unit_of(section_id: str) -> str:
    """섹션 ID의 단원: 앞 두 칸(`PY-03-02` → `PY-03`)."""
    return "-".join(section_id.split("-")[:2])


async def find_anchor(earlier: Sequence[str], *, index: KbIndex | None = None) -> str | None:
    """앞 대화 발화(오래된 순)를 한 질의로 검색한 1위 섹션의 단원. 발화나 결과가 없으면 None.

    본 검색과 같은 `retrieve`(같은 색인·질의 상한)를 k=1로 부른다. 임베딩은 넘기지 않는다:
    닻 질의로 임베딩 API를 한 번 더 부르지 않고 BM25만 쓴다(`toggles.embedding`을 켜도 같다).
    """
    if not earlier:
        return None
    top = await retrieve(earlier[-1], earlier[:-1], k=1, index=index)
    return unit_of(top[0].id) if top else None


def anchor_first(sections: Sequence[Section], unit: str | None) -> tuple[Section, ...]:
    """닻 단원 섹션을 앞으로 옮긴 새 튜플. 같은 쪽 안의 검색 순위는 그대로다(안정 정렬).

    unit이 None이면 순서를 바꾸지 않는다. 입력을 바꾸지 않는다.
    """
    if unit is None:
        return tuple(sections)
    return tuple(sorted(sections, key=lambda s: unit_of(s.id) != unit))
