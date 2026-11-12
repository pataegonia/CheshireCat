"""S2.5 LR 보조 판정기: 기동 학습·결정성·불변·성능 (GR-05, GR-14, ADR-0013)."""

import time
import warnings
from typing import get_args

import numpy as np
import pytest

from guardrail.classifier import LrClassifier
from guardrail.config import load_config
from guardrail.data import LabeledRow, labeled_rows, load_public
from guardrail.eval import cv
from guardrail.schema import Category


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def rows(cfg):
    return labeled_rows(load_public(cfg.paths.public))


@pytest.fixture(scope="module")
def clf(rows, cfg):
    return LrClassifier(rows, cfg)


def _row(i, category, text):
    return LabeledRow(id=i, endpoint="guard", text=text, category=category)


def test_predict_returns_a_known_category(clf, rows):
    for r in rows[::13]:
        got = clf.predict(r.text)
        assert got in get_args(Category)
        assert type(got) is str


def test_learns_separable_toy_set(cfg):
    rs = [_row(f"a{i}", "normal", f"반복문 설명 부탁 {i}") for i in range(5)]
    rs += [_row(f"b{i}", "off_topic", f"오늘 저녁 메뉴 추천 {i}") for i in range(5)]
    clf = LrClassifier(rs, cfg)
    assert clf.predict("반복문 설명") == "normal"
    assert clf.predict("저녁 메뉴 추천") == "off_topic"


def test_uses_lr_config_c(rows):
    a = LrClassifier(rows, load_config(overrides={"input.lr.C": 0.01}))
    b = LrClassifier(rows, load_config(overrides={"input.lr.C": 100.0}))
    assert a._model.C == 0.01
    assert b._model.C == 100.0


def test_same_input_same_output(rows, cfg):
    a, b = LrClassifier(rows, cfg), LrClassifier(rows, cfg)
    assert [a.predict(r.text) for r in rows[::7]] == [b.predict(r.text) for r in rows[::7]]


@pytest.mark.parametrize("c", [1.0, 100.0])
def test_trains_without_convergence_warning(rows, c):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        LrClassifier(rows, load_config(overrides={"input.lr.C": c}))


@pytest.mark.xfail(
    strict=True,
    reason="미정: input.lr.C=1.0이면 LR이 모든 행을 normal로 낸다(5-분할 block F1 0)",
)
def test_fold_predictions_are_not_constant(rows, cfg):
    split = cv.load_splits()
    preds = set()
    for fold in range(split.folds):
        clf = LrClassifier(cv.train_rows(split, rows, fold=fold), cfg)
        preds |= {clf.predict(r.text) for r in rows if split.fold.get(split.cluster[r.id]) == fold}
    assert len(preds) >= 2


def test_trains_on_fold_rows(rows, cfg):
    split = cv.load_splits()
    train = cv.train_rows(split, rows, fold=0)
    clf = LrClassifier(train, cfg)
    assert set(clf._model.classes_) <= {r.category for r in train}


def test_read_only_after_construction(clf):
    with pytest.raises(AttributeError):
        clf._model = None
    with pytest.raises(AttributeError):
        clf.anything_new = 1
    with pytest.raises(AttributeError):
        del clf._model
    assert not clf._vec.idf_.flags.writeable
    assert not clf._model.coef_.flags.writeable
    assert not clf._model.intercept_.flags.writeable


def test_requests_do_not_change_model(rows, cfg):
    clf = LrClassifier(rows, cfg)

    def fingerprint():
        arrays = (clf._model.coef_, clf._model.intercept_, clf._vec.idf_)
        return [a.tobytes() for a in arrays], sorted(clf._vec.vocabulary_.items())

    before = fingerprint()
    for r in rows[::3]:
        clf.predict(r.text + " 처음 보는 요청 문장")
    assert fingerprint() == before


def test_train_400_rows_under_10s(rows, cfg):
    t = time.perf_counter()
    LrClassifier(rows, cfg)
    assert time.perf_counter() - t <= 10


def test_request_p95_under_20ms(clf, rows):
    times = []
    for r in rows:
        t = time.perf_counter()
        clf.predict(r.text)
        times.append(time.perf_counter() - t)
    assert float(np.percentile(times, 95)) <= 0.020
