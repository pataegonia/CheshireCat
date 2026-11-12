"""S2.5 kNN 보조 판정기: few-shot 규칙·다수결·누수·불변 (GR-05, GR-14, ADR-0013)."""

import ast
import time
from pathlib import Path

import numpy as np
import pytest

from guardrail import knn
from guardrail.clusters import build_clusters
from guardrail.config import load_config
from guardrail.data import LabeledRow, labeled_rows, load_public
from guardrail.eval import cv
from guardrail.knn import KnnIndex, select_fewshot
from guardrail.schema import decision_of

SRC = Path(knn.__file__).parent


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def block(cfg):
    return frozenset(cfg.block_categories)


@pytest.fixture(scope="module")
def rows(cfg):
    return labeled_rows(load_public(cfg.paths.public))


@pytest.fixture(scope="module")
def index(rows, cfg):
    return KnnIndex(rows, cfg)


def _row(i, category="normal", text=None):
    return LabeledRow(id=i, endpoint="guard", text=text or f"문장 {i}", category=category)


def _select(ranked, cluster=None, *, max_examples=8, per_category=2, block=None):
    cluster = cluster or {r.id: r.id for r in ranked}
    block = block if block is not None else frozenset({"jailbreak_injection", "off_topic"})
    return select_fewshot(ranked, cluster, block, max_examples, per_category)


ALLOW = ("normal", "answer_seeking", "out_of_material", "pii_disclosure", "emotional_distress")


# --- few-shot 규칙: 순수 함수 (순위를 직접 준다) ---


def test_most_similar_example_is_last():
    ranked = [_row(f"r{i}", c) for i, c in enumerate(ALLOW)]
    ranked.append(_row("b", "off_topic"))
    out = _select(ranked)
    assert out[-1].id == "r0"
    order = {r.id: i for i, r in enumerate(ranked)}
    assert [order[r.id] for r in out] == sorted((order[r.id] for r in out), reverse=True)


def test_rule1_opposite_nearest_kept_even_past_total_cap():
    # 허용 범주 5개 × 2 = 10개가 모두 block 예시보다 가깝다 → 규칙 ①이 없으면 block이 빠진다
    ranked = [_row(f"{c}{k}", c) for c in ALLOW for k in range(2)]
    ranked += [_row("far_block", "jailbreak_injection"), _row("far_block2", "off_topic")]
    out = _select(ranked)
    ids = [r.id for r in out]
    assert "far_block" in ids  # 반대쪽 최근접 1개
    assert "far_block2" not in ids  # 반대쪽 두 번째는 보장 대상이 아님
    assert len(out) == 8


def test_rule1_opposite_relative_to_top1_decision():
    # 1위가 block이면 반대쪽은 가장 가까운 allow
    ranked = [_row("b0", "jailbreak_injection"), _row("b1", "jailbreak_injection")]
    ranked += [_row(f"o{i}", "off_topic") for i in range(2)]
    ranked += [_row("a_far", "normal")]
    out = _select(ranked, max_examples=4)
    assert "a_far" in [r.id for r in out]
    assert out[-1].id == "b0"


def test_no_opposite_when_all_same_decision():
    ranked = [_row(f"r{i}", c) for i, c in enumerate(ALLOW * 2)]
    out = _select(ranked)
    assert len(out) == 8
    assert all(r.category in ALLOW for r in out)


def test_rule2_at_most_two_per_category():
    ranked = [_row(f"n{i}") for i in range(6)] + [_row("x", "answer_seeking")]
    out = _select(ranked)
    assert [r.id for r in out] == ["x", "n1", "n0"]


def test_rule3_at_most_max_examples():
    ranked = [_row(f"r{i}", c) for i, c in enumerate(ALLOW * 3)]
    assert len(_select(ranked)) == 8
    assert len(_select(ranked, max_examples=4)) == 4


def test_rule4_pair_added_when_caps_allow():
    # p0(1위)의 짝 p1은 순위가 멀어도 같이 들어가고, 대신 가장 먼 채움 예시가 빠진다
    ranked = [_row("p0", "normal")] + [_row(f"f{i}", c) for i, c in enumerate(ALLOW[1:] * 2)]
    ranked += [_row("p1", "low_signal_input")]  # allow라 규칙 ①과 무관
    cluster = {r.id: r.id for r in ranked} | {"p1": "P", "p0": "P"}
    ids = [r.id for r in _select(ranked, cluster)]
    assert "p1" in ids
    assert len(ids) == 8
    assert "f6" not in ids
    assert "f7" not in ids


def test_rule4_pair_dropped_when_category_full_but_opposite_stays():
    # n0·n1이 normal 두 칸을 채운 뒤 x의 짝(normal)은 범주 상한을 넘어 빠진다.
    # 반대쪽 최근접 b는 남는다
    ranked = [_row("n0"), _row("n1"), _row("x", "answer_seeking"), _row("b", "jailbreak_injection")]
    ranked += [_row("xm", "normal")]
    cluster = {r.id: r.id for r in ranked} | {"x": "X", "xm": "X"}
    ids = {r.id for r in _select(ranked, cluster)}
    assert "xm" not in ids
    assert {"n0", "n1", "x", "b"} <= ids


def test_rule4_pair_dropped_when_total_full_but_opposite_stays():
    ranked = [_row(f"r{i}", c) for i, c in enumerate(ALLOW)]  # 5
    ranked += [_row("y", "normal"), _row("ym", "answer_seeking")]
    ranked += [_row("b", "off_topic")]
    cluster = {r.id: r.id for r in ranked} | {"r4": "Y", "ym": "Y"}
    out = _select(ranked, cluster, max_examples=6)
    ids = {r.id for r in out}
    # r4 다음 짝 ym을 넣을 차례에 전체 6칸 중 b(1) + r0–r4(5)가 이미 찼다
    assert "ym" not in ids
    assert "b" in ids
    assert len(out) == 6


def test_pair_of_top1_does_not_push_out_top1():
    # 반대쪽 최근접의 짝이 1위와 같은 범주여도 1위가 먼저 들어간다
    ranked = [_row("t"), _row("b", "jailbreak_injection"), _row("bm1"), _row("bm2")]
    cluster = {"t": "t", "b": "B", "bm1": "B", "bm2": "B"}
    out = _select(ranked, cluster)
    assert out[-1].id == "t"


def test_empty_ranked_gives_empty():
    assert _select([]) == []


# --- 실제 400행 색인 ---


def test_fewshot_invariants_on_public_rows(index, rows, cfg, block):
    k = cfg.input.knn
    for r in rows[::7]:
        out = index.fewshot(r.text)
        ranked = index.neighbors(r.text, len(rows))
        pos = {x.id: i for i, x in enumerate(ranked)}
        assert 4 <= len(out) <= k.max_examples
        cats = [x.category for x in out]
        assert max(cats.count(c) for c in cats) <= k.per_category
        assert out[-1].id == ranked[0].id  # 가장 비슷한 것 마지막
        assert [pos[x.id] for x in out] == sorted((pos[x.id] for x in out), reverse=True)
        top = decision_of(ranked[0].category, block)
        opp = next(x for x in ranked if decision_of(x.category, block) != top)
        assert opp.id in {x.id for x in out}


def test_fewshot_only_text_and_category_needed(index, rows):
    out = index.fewshot(rows[0].text)
    assert all(isinstance(x, LabeledRow) and x.text and x.category for x in out)


def test_same_input_same_output(rows, cfg):
    a, b = KnnIndex(rows, cfg), KnnIndex(rows, cfg)
    for r in rows[::11]:
        assert a.fewshot(r.text) == b.fewshot(r.text)
        assert a.majority(r.text) == b.majority(r.text)
        assert a.neighbors(r.text, 5) == b.neighbors(r.text, 5)


def test_neighbors_sorted_by_cosine_then_row_index(cfg):
    # D7: 같은 코사인이면 행 인덱스 순
    rs = [_row("a", "off_topic", "같은 문장이다"), _row("b", "normal", "같은 문장이다")]
    rs.append(_row("c", "normal", "전혀 다른 내용의 질문"))
    idx = KnnIndex(rs, cfg)
    assert [r.id for r in idx.neighbors("같은 문장이다", 3)] == ["a", "b", "c"]
    idx2 = KnnIndex([rs[1], rs[0], rs[2]], cfg)
    assert [r.id for r in idx2.neighbors("같은 문장이다", 2)] == ["b", "a"]


def test_majority_tie_goes_to_first_in_neighbor_list():
    # D7: Counter.most_common 동률 → 이웃 목록에서 먼저 나온 범주
    rs = [
        _row("a", "off_topic", "같은 문장이다"),
        _row("b", "normal", "같은 문장이다"),
        _row("c", "normal", "같은 문장이다"),
        _row("d", "off_topic", "같은 문장이다"),
    ]
    cfg = load_config(overrides={"input.knn_vote_k": 4})
    assert KnnIndex(rs, cfg).majority("같은 문장이다") == "off_topic"
    assert KnnIndex(rs[1:] + rs[:1], cfg).majority("같은 문장이다") == "normal"


def test_majority_uses_knn_vote_k_neighbors():
    rs = [_row("a", "off_topic", "사과 사과 사과")]
    rs += [_row(f"n{i}", "normal", f"사과 사과 바나나 {i}") for i in range(4)]
    one = load_config(overrides={"input.knn_vote_k": 1})
    five = load_config(overrides={"input.knn_vote_k": 5})
    assert KnnIndex(rs, one).majority("사과 사과 사과") == "off_topic"
    assert KnnIndex(rs, five).majority("사과 사과 사과") == "normal"


def test_index_computes_clusters_with_shared_function(index, rows):
    assert index.clusters == build_clusters(rows)


def test_fold_index_has_no_eval_clusters(rows, cfg):
    split = cv.load_splits()
    for fold in range(split.folds):
        train = cv.train_rows(split, rows, fold=fold)
        idx = KnnIndex(train, cfg)
        excluded = set(split.holdout) | {c for c, k in split.fold.items() if k == fold}
        assert idx.rows and all(split.cluster[r.id] not in excluded for r in idx.rows)
        evals = [r for r in rows if split.fold.get(split.cluster[r.id]) == fold]
        for r in evals[::5]:
            got = idx.fewshot(r.text) + idx.neighbors(r.text, cfg.input.knn_vote_k)
            assert all(split.cluster[x.id] != split.cluster[r.id] for x in got)


# --- 불변·성능·금지 ---


def test_read_only_after_construction(index):
    with pytest.raises(AttributeError):
        index.rows = ()
    with pytest.raises(AttributeError):
        index._matrix = None
    with pytest.raises(AttributeError):
        index.anything_new = 1
    assert isinstance(index.rows, tuple)


def test_requests_do_not_change_index(rows, cfg):
    idx = KnnIndex(rows, cfg)
    snap = (idx._matrix.data.copy(), idx._matrix.indices.copy(), dict(idx._vec.vocabulary_))
    idf = idx._vec.idf_.copy()
    for r in rows[::3]:
        idx.fewshot(r.text + " 새로운 요청 문장")
        idx.majority("완전히 처음 보는 질문 xyz")
    assert np.array_equal(idx._matrix.data, snap[0])
    assert np.array_equal(idx._matrix.indices, snap[1])
    assert idx._vec.vocabulary_ == snap[2]
    assert np.array_equal(idx._vec.idf_, idf)
    assert len(idx.rows) == len(rows)
    assert not idx._matrix.data.flags.writeable


def test_build_400_rows_under_10s(rows, cfg):
    t = time.perf_counter()
    KnnIndex(rows, cfg)
    assert time.perf_counter() - t <= 10


def test_request_p95_under_20ms(index, rows):
    times = []
    for r in rows:
        t = time.perf_counter()
        index.fewshot(r.text)
        index.majority(r.text)
        times.append(time.perf_counter() - t)
    assert float(np.percentile(times, 95)) <= 0.020


@pytest.mark.parametrize("name", ["knn.py", "classifier.py"])
def test_no_pickle_fetch_or_eval_import(name):
    src = (SRC / name).read_text(encoding="utf-8")
    assert "fetch_" not in src
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add(node.module or "")
    assert not {m for m in mods if m.split(".")[0] in {"pickle", "joblib", "dill", "cloudpickle"}}
    assert not {m for m in mods if m.startswith("guardrail.eval")}
