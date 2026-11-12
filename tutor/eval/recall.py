"""recall@k 측정(TU-03 수용 기준). 저장소 루트에서 `python -m tutor.eval.recall`.

공개 튜터 행 중 정답 근거(evidence)가 있는 행마다 창 안의 학습자 발화로 `retrieve`를 불러,
정답 섹션이 상위 k개에 있는지 본다. 폴드별 값(가드 `splits/v1.json`), 놓친 행,
검색 지연을 함께 낸다.
질문 본문은 출력하지 않고 행 id와 섹션 id만 낸다.
"""

import asyncio
import json
import math
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from guardrail.eval.cv import load_splits

from tutor.request import parse_request
from tutor.retrieval import default_index, retrieve

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


def is_english(text: str) -> bool:
    """글자 대부분이 라틴 문자인 질의(IN-02가 놓친 행에서 따로 본다)."""
    letters = [ch for ch in text if ch.isalpha()]
    return bool(letters) and sum(ch.isascii() for ch in letters) / len(letters) > 0.5


def public_rows(path: Path = PUBLIC) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines if line.strip()]
    return [row for row in rows if row["labels"].get("evidence")]


async def _rank(rows: Sequence[dict[str, Any]]) -> tuple[list[tuple[str, ...]], list[float]]:
    tops: list[tuple[str, ...]] = []
    latency: list[float] = []
    default_index()  # 서버처럼 색인을 먼저 만든다(지연에는 요청당 검색만 넣는다)
    for row in rows:
        query, history = query_parts(row["messages"])
        start = time.perf_counter()
        sections = await retrieve(query, history, k=max(KS))
        latency.append((time.perf_counter() - start) * 1000)
        tops.append(tuple(section.id for section in sections))
    return tops, latency


def measure(rows: Sequence[dict[str, Any]] | None = None) -> Report:
    rows = public_rows() if rows is None else list(rows)
    if not rows:
        raise ValueError("측정할 행이 없다")
    split = load_splits()
    tops, latency = asyncio.run(_rank(rows))
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


def main() -> int:
    report = measure()
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
