"""S2.5 kNN 보조 판정기: few-shot 예시 고르기·이웃 다수결 (GR-05, GR-14, ADR-0013).

기동 때 공개 행의 마지막 학습자 발화(정규화본)로 문자 n-gram TF-IDF 색인을 만든다. 요청을 처리하는
동안 색인·묶음은 바뀌지 않는다. 짝 묶음은 서버 쪽 clusters.py로 직접 계산한다
(eval 패키지 import 없음).
질의는 호출하는 쪽이 정규화하고 retrieval.max_query_chars로 자른 발화 하나다.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from types import MappingProxyType

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from guardrail.clusters import build_clusters
from guardrail.config import GuardConfig
from guardrail.data import LabeledRow
from guardrail.schema import Category, decision_of


def select_fewshot(
    ranked: Sequence[LabeledRow],
    cluster: Mapping[str, str],
    block: frozenset[Category],
    max_examples: int,
    per_category: int,
) -> list[LabeledRow]:
    """코사인 순위(가까운 순) → few-shot 예시. 가장 비슷한 예시가 마지막(D2).

    우선순위(GR-05): ① 1위와 block/allow가 다른 예시 중 최근접 1개는 반드시 ② 범주당 per_category개
    ③ 전체 max_examples개 ④ 고른 예시의 짝(같은 묶음)은 ②·③을 지킬 때만 그 자리에서 더한다.
    """
    if not ranked:
        return []
    rank = {r.id: i for i, r in enumerate(ranked)}
    members: dict[str, list[LabeledRow]] = {}
    for r in ranked:
        members.setdefault(cluster[r.id], []).append(r)
    picked: list[LabeledRow] = []
    taken: set[str] = set()
    per_cat: Counter[str] = Counter()

    def fits(r: LabeledRow) -> bool:
        return (
            r.id not in taken and per_cat[r.category] < per_category and len(picked) < max_examples
        )

    def add(r: LabeledRow) -> None:
        picked.append(r)
        taken.add(r.id)
        per_cat[r.category] += 1

    top = decision_of(ranked[0].category, block)
    opposite = next((r for r in ranked if decision_of(r.category, block) != top), None)
    if opposite is not None:
        # ① 상한(②·③)보다 앞선다. 자리를 먼저 잡고, 짝은 순위에서 만날 때 더한다
        # (1위를 밀어내지 않게)
        add(opposite)
    for r in ranked:
        if r is not opposite:
            if not fits(r):
                continue
            add(r)
        for m in members[cluster[r.id]]:  # ④ 짝은 ②·③을 지킬 때만
            if fits(m):
                add(m)
    return sorted(picked, key=lambda r: rank[r.id], reverse=True)


class KnnIndex:
    """생성 뒤 읽기 전용. neighbors·majority·fewshot은 색인을 바꾸지 않는다(GR-14)."""

    __slots__ = ("_rows", "_vec", "_matrix", "_clusters", "_block", "_vote_k", "_max", "_per_cat")

    def __init__(self, rows: Sequence[LabeledRow], cfg: GuardConfig) -> None:
        rows = tuple(rows)  # 제너레이터도 한 번만 읽는다
        k = cfg.input.knn
        if cfg.input.knn_vote_k < 1:  # 요청 때 IndexError 대신 기동 때 실패
            raise ValueError("input.knn_vote_k는 1 이상")
        vec = TfidfVectorizer(analyzer=k.analyzer, ngram_range=tuple(k.ngram))
        # 행마다 L2 정규화 → 내적 = 코사인
        matrix = vec.fit_transform([r.text for r in rows]).tocsr()
        # 배열은 쓰기 금지. vocabulary_(dict)는 요청 경로가 transform만 해서 바뀌지 않는다
        for arr in (matrix.data, matrix.indices, matrix.indptr, vec.idf_):
            arr.flags.writeable = False
        init = object.__setattr__
        init(self, "_rows", rows)
        init(self, "_vec", vec)
        init(self, "_matrix", matrix)
        init(self, "_clusters", MappingProxyType(build_clusters(rows)))
        init(self, "_block", frozenset(cfg.block_categories))
        init(self, "_vote_k", cfg.input.knn_vote_k)
        init(self, "_max", k.max_examples)
        init(self, "_per_cat", k.per_category)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("KnnIndex는 생성 뒤 바뀌지 않는다")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("KnnIndex는 생성 뒤 바뀌지 않는다")

    @property
    def rows(self) -> tuple[LabeledRow, ...]:
        return self._rows

    @property
    def clusters(self) -> Mapping[str, str]:
        return self._clusters

    def _ranked(self, text: str) -> list[LabeledRow]:
        sims = (self._matrix @ self._vec.transform([text]).T).toarray().ravel()
        order = np.argsort(-sims, kind="stable")  # D7: (−코사인, 행 인덱스)
        return [self._rows[i] for i in order]

    def neighbors(self, text: str, k: int) -> list[LabeledRow]:
        """코사인 가까운 순 k개(k가 행 수보다 크면 전부)."""
        if k < 0:
            raise ValueError("k는 0 이상")
        return self._ranked(text)[:k]

    def majority(self, text: str) -> Category:
        """이웃 knn_vote_k개의 다수 범주. 동률이면 이웃 목록에서 먼저 나온 범주(D7)."""
        votes = Counter(r.category for r in self.neighbors(text, self._vote_k))
        return votes.most_common(1)[0][0]

    def fewshot(self, text: str) -> list[LabeledRow]:
        return select_fewshot(
            self._ranked(text), self._clusters, self._block, self._max, self._per_cat
        )
