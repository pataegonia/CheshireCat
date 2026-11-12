"""recall@k 측정(TU-03 수용 기준). 저장소 루트에서 `python -m tutor.eval.recall`.

공개 튜터 행 중 정답 근거(evidence)가 있는 행마다 창 안의 학습자 발화로 `retrieve`를 불러,
정답 섹션이 상위 k개에 있는지 본다. 폴드별 값(가드 `splits/v1.json`), 놓친 행,
검색 지연을 함께 낸다.
`--embedding`을 주면 영어 질의에 임베딩 RRF를 섞는다(TU-11). 운영진 임베딩 API를 부르므로
EMBEDDING_API_KEY·EMBEDDING_MODEL이 있어야 하고(런박스), 부르기 전에 예상 호출 수를 낸다.
질문 본문은 출력하지 않고 행 id와 섹션 id만 낸다.
"""

import argparse
import asyncio
import json
import math
import os
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from guardrail.eval.cv import load_splits

from tutor.request import parse_request
from tutor.retrieval import default_index, retrieve
from tutor.retrieval.embedding import BATCH, Embed, EmbeddingSearch, is_english, openai_embed
from tutor.retrieval.kb import bigrams
from tutor.runtime.config import load_config

PUBLIC = Path(__file__).resolve().parents[2] / "competition" / "public" / "tutor.jsonl"
KS = (1, 3, 5)


@dataclass(frozen=True)
class Report:
    rows: int
    row_ids: tuple[str, ...]
    recall: dict[int, float]  # k → recall@k
    by_fold: dict[str, tuple[float, int]]  # 폴드(또는 holdout) → (recall@5, 행 수)
    missed: tuple[
        tuple[str, tuple[str, ...], tuple[str, ...], bool], ...
    ]  # (id, 정답, 상위 5, 영어)
    latency_ms: tuple[float, ...]


def query_parts(messages: Sequence[Any]) -> tuple[str, list[str]]:
    """창 안의 학습자 발화 → (마지막 발화, 그 앞 발화들). 읽을 수 없는 메시지는 건너뛴다.

    서버와 같은 규칙(`TutorRequest.search_query`)을 쓴다. 버튼 문구 빼기는 #97 `tidy`(TU-02)를
    연결하면 그쪽에서 맞춘다.
    """
    req = parse_request({"messages": list(messages)})
    return req.search_query() if req is not None else ("", [])


def public_rows(path: Path = PUBLIC) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines if line.strip()]
    return [row for row in rows if row["labels"].get("evidence")]


Build = Callable[[], Awaitable[EmbeddingSearch]]


async def _rank(
    rows: Sequence[dict[str, Any]], build: Build | None = None
) -> tuple[list[tuple[str, ...]], list[float]]:
    tops: list[tuple[str, ...]] = []
    latency: list[float] = []
    default_index()  # 서버처럼 색인을 먼저 만든다(지연에는 요청당 검색만 넣는다)
    # 임베딩 검색은 측정과 같은 이벤트 루프에서 만든다(API 클라이언트가 루프에 묶인다)
    embedding = await build() if build is not None else None
    for row in rows:
        query, history = query_parts(row["messages"])
        start = time.perf_counter()
        sections = await retrieve(query, history, k=max(KS), embedding=embedding)
        latency.append((time.perf_counter() - start) * 1000)
        tops.append(tuple(section.id for section in sections))
    return tops, latency


def measure(rows: Sequence[dict[str, Any]] | None = None, *, build: Build | None = None) -> Report:
    """build(임베딩 검색을 만드는 함수)를 주면 영어 질의에 임베딩 RRF를 섞어 잰다(TU-11)."""
    rows = public_rows() if rows is None else list(rows)
    if not rows:
        raise ValueError("측정할 행이 없다")
    split = load_splits()
    tops, latency = asyncio.run(_rank(rows, build))
    golds = [tuple(row["labels"]["evidence"]) for row in rows]
    hits = {
        k: [any(g in top[:k] for g in gold) for gold, top in zip(golds, tops, strict=True)]
        for k in KS
    }
    folds: dict[str, list[bool]] = {}
    for row, hit in zip(rows, hits[5], strict=True):
        cluster = split.cluster[row["id"]]
        fold = "holdout" if cluster in split.holdout else str(split.fold[cluster])
        folds.setdefault(fold, []).append(hit)
    missed = tuple(
        (row["id"], gold, top, is_english(query_parts(row["messages"])[0]))
        for row, gold, top, hit in zip(rows, golds, tops, hits[5], strict=True)
        if not hit
    )
    return Report(
        rows=len(rows),
        row_ids=tuple(row["id"] for row in rows),
        recall={k: sum(v) / len(rows) for k, v in hits.items()},
        by_fold={fold: (sum(v) / len(v), len(v)) for fold, v in sorted(folds.items())},
        missed=missed,
        latency_ms=tuple(latency),
    )


def _percentile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]


class _Counted:
    """임베딩 호출 수와 실패 수(오류·시간 초과)를 센다(TU-16 호출 수 보고)."""

    def __init__(self, embed: Embed) -> None:
        self.embed = embed
        self.calls = 0
        self.failures = 0

    async def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        try:
            return await self.embed(texts)
        except BaseException:  # 시간 초과(취소)도 센다. 예외는 그대로 올린다
            self.failures += 1
            raise


class KbEmbeddingError(RuntimeError):
    """KB 임베딩을 만들지 못했다(클래스 이름만 담는다)."""


def _english_queries(rows: Sequence[dict[str, Any]]) -> int:
    """임베딩을 부르는 질의 수(retrieve와 같은 조건: 영어 위주이고 2-gram이 있다)."""
    queries = (query_parts(row["messages"])[0] for row in rows)
    return sum(1 for q in queries if is_english(q) and bigrams(q))


def _embedding(ap: argparse.ArgumentParser) -> tuple[Build, _Counted]:
    """임베딩 검색을 만드는 함수를 준비한다. 부르기 전에 예상 호출 수(KB 묶음 + 영어 질의)를
    내고 상한을 본다.
    """
    if not (os.environ.get("EMBEDDING_API_KEY") and os.environ.get("EMBEDDING_MODEL")):
        ap.error("--embedding에는 EMBEDDING_API_KEY·EMBEDDING_MODEL이 필요하다(런박스)")
    cfg = load_config()
    index = default_index()
    expected = math.ceil(len(index.chunks) / BATCH) + _english_queries(public_rows())
    sys.stdout.write(f"임베딩 예상 호출 {expected}회(상한 {cfg.eval.max_calls}회)\n")
    if expected > cfg.eval.max_calls:
        ap.error(f"임베딩 예상 호출 {expected}회가 상한 {cfg.eval.max_calls}회를 넘는다")
    counted = _Counted(openai_embed())

    async def build() -> EmbeddingSearch:
        try:
            return await EmbeddingSearch.build(
                index, counted, timeout_s=cfg.llm.timeout_s.embedding
            )
        except Exception as exc:  # 메시지는 내지 않는다
            raise KbEmbeddingError(type(exc).__name__) from None

    return build, counted


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="recall@k 측정(TU-03·TU-11)")
    ap.add_argument(
        "--embedding", action="store_true", help="영어 질의에 임베딩 RRF(운영진 임베딩 API)"
    )
    args = ap.parse_args(argv)
    build, counted = _embedding(ap) if args.embedding else (None, None)
    try:
        report = measure(build=build)
    except KbEmbeddingError as exc:
        ap.error(f"KB 임베딩을 만들지 못했다: {exc}")
    r, lat, out = report.recall, report.latency_ms, sys.stdout
    out.write(f"recall@1 {r[1]:.3f} · @3 {r[3]:.3f} · @5 {r[5]:.3f}")
    out.write(f" (정답 근거가 있는 공개 행 {report.rows}개)\n")
    folds = ", ".join(f"{fold} {value:.3f}({n})" for fold, (value, n) in report.by_fold.items())
    out.write(f"폴드별 recall@5: {folds}\n")
    p50, p95 = _percentile(lat, 0.5), _percentile(lat, 0.95)
    out.write(f"검색 지연 ms: p50 {p50:.2f} · p95 {p95:.2f} · 최대 {max(lat):.2f}\n")
    out.write(f"놓친 행(@5) {len(report.missed)}개\n")
    for row_id, gold, top, english in report.missed:
        tag = " [영어]" if english else ""
        out.write(f"- {row_id}{tag}: 정답 {','.join(gold)} → 상위 5 {', '.join(top) or '(없음)'}\n")
    if counted is not None:
        out.write(f"임베딩 호출 {counted.calls}회(실패 {counted.failures}회)\n")
        if counted.failures:  # 실패한 질의는 BM25 순위로 쟀다
            out.write("질의 임베딩이 실패한 행은 BM25로 쟀다: 이 결과는 임베딩 효과가 아니다\n")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
