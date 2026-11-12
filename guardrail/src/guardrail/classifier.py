"""S2.5 LR 보조 판정기 (GR-05, GR-14, ADR-0013).

기동 때 공개 행의 마지막 학습자 발화(정규화본)로 문자 n-gram TF-IDF + LogisticRegression을 학습한다.
저장한 모델 파일은 쓰지 않고(pickle 없음) 요청으로 모델을 갱신하지 않는다.
"""

from collections.abc import Sequence
from typing import cast

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression

from guardrail.config import GuardConfig
from guardrail.data import LabeledRow
from guardrail.schema import Category

MAX_ITER = 1000  # lbfgs 기본 100회로는 400행 10범주에서 수렴 경고가 날 수 있다


class LrClassifier:
    """생성 뒤 읽기 전용. predict는 모델을 바꾸지 않는다(GR-14)."""

    __slots__ = ("_vec", "_model")

    def __init__(self, rows: Sequence[LabeledRow], cfg: GuardConfig) -> None:
        c = cfg.input.lr
        vec = TfidfVectorizer(analyzer=c.analyzer, ngram_range=tuple(c.ngram))
        model = LogisticRegression(C=c.C, max_iter=MAX_ITER)
        model.fit(vec.fit_transform([r.text for r in rows]), [r.category for r in rows])
        for arr in (model.coef_, model.intercept_, vec.idf_):
            arr.flags.writeable = False
        object.__setattr__(self, "_vec", vec)
        object.__setattr__(self, "_model", model)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("LrClassifier는 생성 뒤 바뀌지 않는다")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("LrClassifier는 생성 뒤 바뀌지 않는다")

    def predict(self, text: str) -> Category:
        return cast(Category, str(self._model.predict(self._vec.transform([text]))[0]))
