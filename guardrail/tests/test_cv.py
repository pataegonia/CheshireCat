"""S1.4 짝 묶음·분할: 누수 없는 5폴드 + 홀드아웃 (ADR-0013, master-plan §4.5)."""

import json
from collections import Counter

import pytest

from guardrail.clusters import BUTTON_PHRASES, build_clusters
from guardrail.config import load_config
from guardrail.data import LabeledRow, labeled_rows, load_public
from guardrail.eval import cv

PAIR = ("pub-57a7f24f", "pub-55a2bbad")  # R13-A §1.1 "최대 ↔ 최소"


@pytest.fixture(scope="module")
def rows():
    return labeled_rows(load_public(load_config().paths.public))


@pytest.fixture(scope="module")
def clusters(rows):
    return build_clusters(rows)


@pytest.fixture(scope="module")
def split(rows):
    return cv.make_splits(rows, seed=cv.DEFAULT_SEED)


def _row(i, text, endpoint="guard"):
    return LabeledRow(id=i, endpoint=endpoint, text=text, category="normal")


def test_min_max_pair_shares_cluster(clusters):
    assert clusters[PAIR[0]] == clusters[PAIR[1]]


def test_every_row_has_a_cluster(rows, clusters):
    assert set(clusters) == {r.id for r in rows}


def test_unrelated_texts_stay_apart():
    c = build_clusters(
        [_row("a", "정기예금 최소 금액이 얼마예요"), _row("b", "파이썬 반복문 설명")]
    )
    assert c["a"] != c["b"]


def test_button_phrase_alone_does_not_link_rows():
    phrase = BUTTON_PHRASES[0]
    c = build_clusters([_row("a", phrase), _row("b", phrase)])
    assert c["a"] != c["b"]


def test_all_eight_button_phrases_are_excluded():
    assert len(BUTTON_PHRASES) == 8
    rows = [_row(f"r{i}", p) for i, p in enumerate(BUTTON_PHRASES)]
    rows += [_row(f"s{i}", p) for i, p in enumerate(BUTTON_PHRASES)]
    assert len(set(build_clusters(rows).values())) == 16


def test_button_prefix_with_different_tail_does_not_link_on_prefix():
    phrase = BUTTON_PHRASES[1]
    c = build_clusters([_row("a", phrase + " 반복문이요"), _row("b", phrase + " 은행 이체요")])
    assert c["a"] != c["b"]


def test_near_duplicates_link_even_with_button_prefix():
    phrase = BUTTON_PHRASES[1]
    tail = " 정기예금 최소 가입 금액이 얼마인지 알려 주세요"
    c = build_clusters([_row("a", phrase + tail), _row("b", phrase + tail + "?")])
    assert c["a"] == c["b"]


def test_short_text_and_empty_text_are_safe():
    c = build_clusters([_row("a", "ab"), _row("b", "ab"), _row("c", ""), _row("d", "")])
    assert c["a"] == c["b"]
    assert c["c"] != c["d"]


def test_cluster_ids_are_independent_of_row_order(rows):
    assert build_clusters(rows) == build_clusters(list(reversed(rows)))


def test_cluster_never_spans_two_folds(rows, clusters, split):
    assert split.cluster == clusters
    assert set(split.fold) | set(split.holdout) == set(clusters.values())
    assert not set(split.fold) & set(split.holdout)  # 홀드아웃과 폴드가 겹치지 않음
    assert set(split.fold.values()) == set(range(5))


def test_folds_are_balanced_on_guard_rows(rows, split):
    per_fold = Counter(
        split.fold[split.cluster[r.id]]
        for r in rows
        if r.endpoint == "guard" and split.cluster[r.id] in split.fold
    )
    assert max(per_fold.values()) - min(per_fold.values()) <= 1


def test_holdout_is_about_20_percent_of_guard_rows_with_no_tutor_only_cluster(rows, split):
    guard = [r for r in rows if r.endpoint == "guard"]
    held = [r for r in guard if split.cluster[r.id] in split.holdout]
    assert abs(len(held) - 0.2 * len(guard)) <= 1
    for cid in split.holdout:
        assert any(r.endpoint == "guard" for r in rows if split.cluster[r.id] == cid)


def test_same_seed_same_bytes_and_other_seed_differs(rows):
    a = cv.make_splits(rows, seed=13)
    assert cv.dumps(a) == cv.dumps(cv.make_splits(rows, seed=13))
    other = cv.make_splits(rows, seed=14)
    assert (a.fold, a.holdout) != (other.fold, other.holdout)  # seed가 실제로 배정을 바꾼다


def test_train_rows_exclude_eval_fold_and_holdout(rows, split):
    for k in range(5):
        train = cv.train_rows(split, rows, fold=k)
        bad = {cid for cid, f in split.fold.items() if f == k} | set(split.holdout)
        assert train and all(split.cluster[r.id] not in bad for r in train)


def test_train_rows_for_holdout_only_drop_holdout(rows, split):
    train = cv.train_rows(split, rows, holdout=True)
    assert all(split.cluster[r.id] not in split.holdout for r in train)
    assert len(train) == sum(split.cluster[r.id] not in split.holdout for r in rows)


def test_train_rows_needs_exactly_one_target_and_known_rows(rows, split):
    with pytest.raises(ValueError):
        cv.train_rows(split, rows)
    with pytest.raises(ValueError):
        cv.train_rows(split, rows, fold=0, holdout=True)
    with pytest.raises(ValueError):
        cv.train_rows(split, rows, fold=9)
    with pytest.raises(ValueError, match="zzz"):
        cv.train_rows(split, [_row("zzz", "새 행")], fold=0)


def test_committed_v1_matches_a_fresh_build(rows, split):
    committed = cv.load_splits()
    assert committed.version == "v1"
    assert committed == split
    on_disk = cv.SPLIT_PATH.read_bytes().replace(b"\r\n", b"\n")  # autocrlf 무시
    assert on_disk == cv.dumps(split), "normalize()가 바뀌었다면 v1을 고치지 말고 v2로 올린다"


def test_write_is_idempotent(tmp_path):
    out = tmp_path / "v1.json"
    cv.write_splits(out)
    first = out.read_bytes()
    cv.write_splits(out)
    assert out.read_bytes() == first
    assert json.loads(first)["version"] == "v1"


def test_cli_prints_summary(capsys):
    assert cv.main([]) == 0
    assert "largest" in capsys.readouterr().out


def test_real_data_is_not_over_merged(clusters):
    sizes = Counter(clusters.values())
    assert len(sizes) >= 300
    assert max(sizes.values()) <= 5


def test_threshold_is_inclusive_and_chains_are_transitive():
    a, b = _row("a", "abcdef"), _row("b", "abcdeg")  # 3-gram 4개 중 3개 공유 → 0.6
    assert build_clusters([a, b], jaccard=0.6)["a"] == build_clusters([a, b], jaccard=0.6)["b"]
    c = build_clusters([a, b], jaccard=0.61)
    assert c["a"] != c["b"]
    chain = [_row("c", "abcdefgh"), _row("b", "bcdefghi"), _row("a", "cdefghij")]
    out = build_clusters(chain, jaccard=0.5)
    assert len(set(out.values())) == 1 and set(out.values()) == {"a"}  # 사전순 첫 id


def test_fold_bounds():
    split = cv.make_splits([_row("a", "하나"), _row("b", "둘")], folds=2, holdout=0.0)
    for bad in (-1, 2):
        with pytest.raises(ValueError):
            cv.train_rows(split, [_row("a", "하나")], fold=bad)
