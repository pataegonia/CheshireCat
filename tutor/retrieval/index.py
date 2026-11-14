"""KB 색인(TU-03). BM25는 가드 `KbIndex`(GR-03)를 그대로 쓰고(TD-03) 튜터는 섹션으로 접는다.

조각·토큰·점수는 가드 색인이 정하고, n-gram·k1·b와 질의 상한은 가드 설정(`kb`·`retrieval`)을
따른다. 튜터는 섹션 정보(과목·제목·표)를 따로 들고 조각 점수를 섹션 최고 점수로 접는다.
색인은 기동 때 한 번 만들고 요청 처리 중에는 바뀌지 않는다(TU-18).
"""

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from guardrail.config import load_config
from guardrail.kb import KbIndex as GuardKbIndex

from tutor.retrieval.kb import Section, load_sections


class KbIndex:
    """KB 섹션과 가드 BM25 색인. `search`는 조각 점수를 섹션 최고 점수로 접는다."""

    def __init__(self, sections: Mapping[str, Section]) -> None:
        """섹션이 없거나 색인할 2-gram이 없으면 가드 색인이 ValueError를 낸다."""
        cfg = load_config()
        self.sections: Mapping[str, Section] = MappingProxyType(dict(sections))
        self._bm25 = GuardKbIndex(
            {sid: section.full_text() for sid, section in self.sections.items()},
            ngram=cfg.kb.ngram,
            k1=cfg.kb.k1,
            b=cfg.kb.b,
        )
        self.chunks: tuple[tuple[str, str], ...] = self._bm25.chunks
        # 가드 색인은 질의를 자르지 않는다(호출자 몫). 넘으면 뒤쪽(최근 발화)을 남긴다
        self.max_query_chars: int = cfg.retrieval.max_query_chars

    @classmethod
    def from_dir(cls, kb_dir: Path) -> "KbIndex":
        """KB 폴더에서 만든다. 섹션이 하나도 없으면 기동을 멈추도록 예외를 낸다."""
        sections = load_sections(kb_dir)
        if not sections:
            raise FileNotFoundError(f"KB 섹션(*.md)을 찾지 못했다: {kb_dir}")
        return cls(sections)

    def chunk_scores(self, query: str) -> list[float]:
        """조각마다 BM25 점수(조각 순서). 가드 `KbIndex.scores` 그대로다."""
        return self._bm25.scores(query)

    def search(self, query: str, *, subject: str | None = None) -> list[tuple[str, float]]:
        """점수가 0보다 큰 섹션을 (섹션 ID, 최고 조각 점수)로 점수 높은 순(같으면 ID 순)."""
        best: dict[str, float] = {}
        for (sid, _text), score in zip(self.chunks, self.chunk_scores(query), strict=True):
            if score > best.get(sid, 0.0):
                best[sid] = score
        ranked = sorted(best.items(), key=lambda item: (-item[1], item[0]))
        if subject is None:
            return ranked
        return [(sid, score) for sid, score in ranked if self.sections[sid].subject == subject]

    def section(self, sid: str) -> Section:
        return self.sections[sid]

    def section_text(self, sid: str) -> str:
        """섹션 원문 전체(md + CSV). 근거 묶음에 넣는다."""
        return self.sections[sid].full_text()
