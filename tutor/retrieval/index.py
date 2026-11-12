"""문자 2-gram BM25 KB 색인(TU-03). 가드 R13 `BM25`와 같은 점수를 낸다(테스트가 맞춰 본다).

질의 2-gram은 종류만 센다(같은 2-gram이 여러 번 나와도 한 번). 조각마다 2-gram 가중치를 미리 계산해
두어 질의 때는 덧셈만 한다. 색인은 기동 때 한 번 만들고 요청 처리 중에는 바뀌지 않는다(TU-18).
"""

import math
from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from tutor.retrieval.kb import Section, bigrams, load_sections, split_chunks

K1 = 1.2  # 가드 설정 kb.k1과 같다
B = 0.75  # 가드 설정 kb.b와 같다


class KbIndex:
    """KB 섹션과 그 조각의 BM25 색인. `search`는 조각 점수를 섹션 최고 점수로 접는다."""

    def __init__(self, sections: Mapping[str, Section], *, k1: float = K1, b: float = B) -> None:
        if not sections:
            raise ValueError("KB 섹션이 없다")
        self.sections: Mapping[str, Section] = MappingProxyType(dict(sections))
        self.chunks: tuple[tuple[str, str], ...] = tuple(
            (sid, part)
            for sid, section in self.sections.items()
            for part in split_chunks(section.full_text())
        )
        tfs = [Counter(bigrams(text)) for _sid, text in self.chunks]
        lens = [sum(tf.values()) for tf in tfs]
        avg = sum(lens) / len(lens) or 1.0
        df = Counter(gram for tf in tfs for gram in tf)
        n = len(tfs)
        postings: dict[str, list[tuple[int, float]]] = defaultdict(list)
        for i, (tf, length) in enumerate(zip(tfs, lens, strict=True)):
            norm = k1 * (1 - b + b * length / avg)
            for gram, f in tf.items():
                idf = math.log(1 + (n - df[gram] + 0.5) / (df[gram] + 0.5))
                postings[gram].append((i, idf * f * (k1 + 1) / (f + norm)))
        self._postings: Mapping[str, tuple[tuple[int, float], ...]] = MappingProxyType(
            {gram: tuple(entries) for gram, entries in postings.items()}
        )

    @classmethod
    def from_dir(cls, kb_dir: Path) -> "KbIndex":
        """KB 폴더에서 만든다. 섹션이 하나도 없으면 기동을 멈추도록 예외를 낸다."""
        sections = load_sections(kb_dir)
        if not sections:
            raise FileNotFoundError(f"KB 섹션(*.md)을 찾지 못했다: {kb_dir}")
        return cls(sections)

    def chunk_scores(self, query: str) -> list[float]:
        """조각마다 BM25 점수(조각 순서). 질의 2-gram은 처음 나온 순서로 한 번씩 더한다."""
        scores = [0.0] * len(self.chunks)
        for gram in dict.fromkeys(bigrams(query)):
            for i, weight in self._postings.get(gram, ()):
                scores[i] += weight
        return scores

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
