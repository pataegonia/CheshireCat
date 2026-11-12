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
from types import MappingProxyType

from guardrail.config import resolve_path

# competition/grader/scoring.py의 PUNCT·normalize 복사(런타임에 grader를 import하지 않음)
SECTION_ID = re.compile(r"[A-Z]+-\d{2}-\d{2}")  # SPEC §1 형식(PY-01-02). 과목은 가정하지 않음
PUNCT = frozenset(".,!?;:'\"“”‘’`()[]{}<>«»·…~-–—/\\")


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
        if not SECTION_ID.fullmatch(
            md.stem
        ):  # README.md 같은 다른 md가 섞이면 순위·margin이 바뀐다
            raise ValueError(f"KB에 섹션 ID 형식이 아닌 md가 있다: {md.parent.name}/{md.name}")
        tables = sorted(md.parent.glob(f"{md.stem}-table*.csv"))
        parts = [md.read_text(encoding="utf-8"), *(t.read_text(encoding="utf-8") for t in tables)]
        if not parts[0].strip():
            raise ValueError(f"KB 섹션 md가 비었다: {md.stem}")
        if md.stem in sections:  # 다른 과목 폴더에 같은 섹션 ID가 있으면 한쪽이 사라진다
            raise ValueError(f"KB 섹션 ID가 겹친다: {md.stem}")
        sections[md.stem] = "\n\n".join(parts)
    return sections


class KbIndex:
    """생성 뒤 읽기 전용. rank()는 요청당 BM25 점수를 한 번만 계산한다."""

    def __init__(
        self, sections: Mapping[str, str], *, ngram: int = 2, k1: float = 1.2, b: float = 0.75
    ) -> None:
        valid_n = isinstance(ngram, int) and not isinstance(ngram, bool) and ngram >= 1
        if not (valid_n and math.isfinite(k1) and k1 >= 0 and 0 <= b <= 1):
            raise ValueError("kb 설정 값이 범위를 벗어났다(ngram ≥ 1, k1 ≥ 0, 0 ≤ b ≤ 1)")
        self._sections = MappingProxyType(dict(sections))
        self._chunks: tuple[tuple[str, str], ...] = tuple(
            (sid, part)
            for sid, text in self._sections.items()
            for part in re.split(r"\n(?=## )", text)
        )
        self._n = ngram
        tfs = [Counter(_grams(text, ngram)) for _, text in self._chunks]
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
        self._postings = MappingProxyType({g: tuple(p) for g, p in postings.items()})

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

    @property
    def chunks(self) -> tuple[tuple[str, str], ...]:
        """(섹션 ID, 조각 텍스트). 섹션 순서·섹션 안 순서 그대로."""
        return self._chunks

    def scores(self, query: str) -> list[float]:
        """조각 순서대로의 BM25 점수. 질의의 서로 다른 n-gram을 처음 나온 순서로 더한다.

        질의 길이는 호출자가 retrieval.max_query_chars로 자른다(guard.py).
        숨은 문자(ZWSP 등)는 여기서 지우지 않는다.
        호출자가 preprocess.normalize를 거친 텍스트를 넘긴다
        (그대로 넘기면 2-gram이 끊겨 모든 조각이 0점일 수 있다).
        """
        if not isinstance(query, str):
            raise TypeError("질의는 str이어야 한다")
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
        if k < 1:
            raise ValueError("k는 1 이상이어야 한다")
        return list(self.rank(query).hits[:k])

    def section_text(self, section_id: str) -> str:
        """Hit.section_id로 부른다. 없는 ID면 KeyError."""
        return self._sections[section_id]
