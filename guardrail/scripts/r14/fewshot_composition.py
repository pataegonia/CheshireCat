"""R14-A: few-shot 예시 구성 통계(API 0회).

실행: `uv run python -X utf8 scripts/r14/fewshot_composition.py` (guardrail/ 폴더).
`guardrail.knn`(S2.5, PR #132)이 있어야 돈다. 검색 변형은 TfidfVectorizer 인자를 바꿔 끼워 잰다
(분석 전용. 서버 코드는 설정 `input.knn`만 읽는다).

두 경계를 잰다.
- cv : 평가 행의 짝 묶음 전체를 뺀 색인(하네스 방식, 짝이 안 보임)
- loo: 400행 색인에서 자기 자신만 뺌(비공개 문항이 공개 틀의 변형일 때에 가까움, 짝이 보임)
평가 행은 둘 다 홀드아웃을 뺀 320행.
"""

import functools
from collections import Counter
from pathlib import Path

root = Path(__file__).resolve().parents[2]

from sklearn.feature_extraction.text import TfidfVectorizer

from guardrail import knn
from guardrail.config import load_config
from guardrail.data import labeled_rows, load_public
from guardrail.eval import cv

base_cfg = load_config(root / "configs" / "default.yaml")
rows = labeled_rows(load_public(root.parent / "competition" / "public"))
split = cv.load_splits(root / "src" / "guardrail" / "eval" / "splits" / "v1.json")
BLOCK = frozenset(base_cfg.block_categories)
hold = set(split.holdout)
eval_rows = [r for r in rows if split.cluster[r.id] not in hold]


def shots_cv(cfg):
    out = {}
    for k in range(split.folds):
        idx = knn.KnnIndex(cv.train_rows(split, rows, fold=k), cfg)
        for r in eval_rows:
            if split.fold[split.cluster[r.id]] == k:
                out[r.id] = idx.fewshot(r.text)
    return out


def shots_loo(cfg):
    idx = knn.KnnIndex(rows, cfg)
    k = cfg.input.knn
    out = {}
    for r in eval_rows:
        ranked = [x for x in idx._ranked(r.text) if x.id != r.id]
        out[r.id] = knn.select_fewshot(ranked, idx.clusters, BLOCK, k.max_examples, k.per_category)
    return out


def report(label, shots):
    n = Counter()
    for r in eval_rows:
        s = shots[r.id]
        cats = [x.category for x in s]
        side = r.category in BLOCK
        sides = [c in BLOCK for c in cats]
        pair = any(split.cluster[x.id] == split.cluster[r.id] for x in s)
        g = "B" if side else "A"
        n[g, "n"] += 1
        n[g, "has_cat"] += r.category in cats
        n[g, "last_block"] += sides[-1]
        n[g, "n_block"] += sum(sides)
        n[g, "pair"] += pair
        n[g, "last_same_cat"] += cats[-1] == r.category
        if r.category != "normal":
            n["nn", "n"] += 1
            n["nn", "has_cat"] += r.category in cats
    b, a = n["B", "n"], n["A", "n"]
    print(
        f"{label:34s} nn_has {n['nn', 'has_cat']:3d}/{n['nn', 'n']} | "
        f"block행 {b}: 끝=block {n['B', 'last_block']:2d} 끝=정답범주 {n['B', 'last_same_cat']:2d} "
        f"block예시 평균 {n['B', 'n_block'] / b:.2f} 짝보임 {n['B', 'pair']:2d} | "
        f"allow행 {a}: 끝=block {n['A', 'last_block']:2d} block예시 평균 {n['A', 'n_block'] / a:.2f} "
        f"짝보임 {n['A', 'pair']:3d}"
    )


VARIANTS = {
    "char_wb 1-3 (기본)": ({}, {}),
    "char_wb 1-3 sublinear": ({"sublinear_tf": True}, {}),
    "char_wb 2-3": ({}, {"ngram": [2, 3]}),
    "char_wb 2-3 sublinear": ({"sublinear_tf": True}, {"ngram": [2, 3]}),
    "char_wb 2-4 sublinear": ({"sublinear_tf": True}, {"ngram": [2, 4]}),
    "char 1-3 sublinear": ({"sublinear_tf": True}, {"analyzer": "char"}),
    "기본, per_category 3": ({}, {"per_category": 3}),
    "기본, max 4": ({}, {"max_examples": 4}),
    "sublinear, max 4": ({"sublinear_tf": True}, {"max_examples": 4}),
}

for mode, fn in (("cv", shots_cv), ("loo", shots_loo)):
    print(f"== {mode}")
    for label, (tfidf_kw, knn_kw) in VARIANTS.items():
        knn.TfidfVectorizer = functools.partial(TfidfVectorizer, **tfidf_kw)
        k = base_cfg.input.knn.model_copy(update=knn_kw)
        cfg = base_cfg.model_copy(update={"input": base_cfg.input.model_copy(update={"knn": k})})
        report(label, fn(cfg))
