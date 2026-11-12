"""S2.2 KB 색인·검색 (GR-03, ADR-0010, master-plan §4.1).

scripts/r13/common.py의 kb_chunks·BM25(R13-A가 잰 변형)를 옮겼다(import하지 않음).
- 섹션 = kb/<과목>/<섹션 ID>.md + 같은 이름의 -table<n>.csv("\\n\\n"으로 이음). HTML은 읽지 않는다.
- 조각 = 섹션 텍스트를 '## '로 시작하는 줄 앞에서 자른 것.
  코드 블록 안에서도 자르고, 첫 '## ' 앞의 '# 제목' 머리말도 한 조각이다.
- BM25 = scoring.normalize 뒤 문자 n-gram. 질의 n-gram의 빈도는 무시한다(서로 다른 n-gram만 합산).
  output.rule_min_margin이 절대 점수에 걸리므로 이 성질을 바꾸지 않는다.
점수는 n-gram → (조각, 가중치) 역색인으로 계산하고, 조각마다 원본과 같은 순서로 더해 값이 같다.
"""

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from guardrail.config import resolve_path

# competition/grader/scoring.py의 PUNCT·normalize 복사(런타임에 grader를 import하지 않음)
PUNCT = set(".,!?;:'\"“”‘’`()[]{}<>«»·…~-–—/\\")


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", str(s)).lower()
    return "".join(ch for ch in s if not ch.isspace() and ch not in PUNCT)


def _grams(text: str, n: int) -> list[str]:
    s = normalize(text)
    return [s[i : i + n] for i in range(len(s) - n + 1)]


@dataclass(frozen=True)
class Hit:
    section_id: str
    text: str
    score: float


@dataclass(frozen=True)
class Ranking:
    hits: tuple[Hit, ...]  # 점수 내림차순 전체 조각(같은 점수는 조각 순서). 상위 k = hits[:k]
    margin: float  # 섹션별 최고 점수로 접은 뒤 1위 섹션 − 다른 섹션 중 최고(전체 순위 기준)


def _load_sections(kb_dir: Path) -> dict[str, str]:
    sections = {}
    for md in sorted(kb_dir.glob("*/*.md")):
        tables = sorted(md.parent.glob(f"{md.stem}-table*.csv"))
        parts = [md.read_text(encoding="utf-8"), *(t.read_text(encoding="utf-8") for t in tables)]
        sections[md.stem] = "\n\n".join(parts)
    return sections


class KbIndex:
    """생성 뒤 읽기 전용. rank()는 요청당 BM25 점수를 한 번만 계산한다."""

    def __init__(
        self, sections: Mapping[str, str], *, ngram: int = 2, k1: float = 1.2, b: float = 0.75
    ) -> None:
        if ngram < 1 or k1 < 0 or not 0 <= b <= 1:
            raise ValueError("kb 설정 값이 범위를 벗어났다(ngram ≥ 1, k1 ≥ 0, 0 ≤ b ≤ 1)")
        self._sections = dict(sections)
        self.chunks: tuple[tuple[str, str], ...] = tuple(
            (sid, part)
            for sid, text in self._sections.items()
            for part in re.split(r"\n(?=## )", text)
        )
        self._n = ngram
        tfs = [Counter(_grams(text, ngram)) for _, text in self.chunks]
        lens = [sum(tf.values()) for tf in tfs]
        total = sum(lens)
        if not total:
            raise ValueError("KB에 색인할 내용이 없다")
        avg = total / len(lens)
        df = Counter(g for tf in tfs for g in tf)
        n = len(tfs)
        postings: dict[str, list[tuple[int, float]]] = {}
        for i, (tf, ln) in enumerate(zip(tfs, lens, strict=True)):
            for g, f in tf.items():
                idf = math.log(1 + (n - df[g] + 0.5) / (df[g] + 0.5))
                w = idf * f * (k1 + 1) / (f + k1 * (1 - b + b * ln / avg))
                postings.setdefault(g, []).append((i, w))
        self._postings = {g: tuple(p) for g, p in postings.items()}

    @classmethod
    def load(
        cls, kb_dir: str | Path, *, ngram: int = 2, k1: float = 1.2, b: float = 0.75
    ) -> "KbIndex":
        base = resolve_path(kb_dir)
        if not base.is_dir():
            raise FileNotFoundError(f"KB 폴더가 없다: {base.name}")
        sections = _load_sections(base)
        if not sections:
            raise ValueError("KB 폴더에 <과목>/<섹션 ID>.md가 없다")
        return cls(sections, ngram=ngram, k1=k1, b=b)

    def scores(self, query: str) -> list[float]:
        """조각 순서대로의 BM25 점수. 질의의 서로 다른 n-gram을 처음 나온 순서로 더한다."""
        s = [0.0] * len(self.chunks)
        for g in dict.fromkeys(_grams(query, self._n)):
            for i, w in self._postings.get(g, ()):
                s[i] += w
        return s

    def rank(self, query: str) -> Ranking:
        s = self.scores(query)
        order = sorted(range(len(s)), key=lambda i: -s[i])
        hits = tuple(Hit(self.chunks[i][0], self.chunks[i][1], s[i]) for i in order)
        best: dict[str, float] = {}
        for h in hits:
            best.setdefault(h.section_id, h.score)  # 내림차순이라 처음 본 값이 섹션 최고
        top = hits[0].section_id
        other = max((v for sid, v in best.items() if sid != top), default=0.0)
        return Ranking(hits, best[top] - other)

    def search(self, query: str, k: int = 3) -> list[Hit]:
        return list(self.rank(query).hits[:k])

    def section_text(self, section_id: str) -> str:
        return self._sections[section_id]
