"""recall@k 측정(TU-03 수용 기준). 저장소 루트에서 `python -m tutor.eval.recall`.

공개 튜터 행 중 정답 근거(evidence)가 있는 행마다 창 안의 학습자 발화로 `retrieve`를 불러,
정답 섹션이 상위 k개에 있는지 본다. 폴드별 값(가드 `splits/v1.json`), 놓친 행,
검색 지연을 함께 낸다. 홀드아웃 묶음 행은 재지 않는다(통합 하네스에서 최대 3회, TU-16·#84).
`--embedding`을 주면 영어 질의에 임베딩 RRF를 섞는다(TU-11). 운영진 임베딩 API를 부르므로
EMBEDDING_API_KEY·EMBEDDING_MODEL·EMBEDDING_BASE_URL이 있어야 하고(런박스), 부르기 전에
예상 호출 수를 낸다.
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
EMBEDDING_ENV = ("EMBEDDING_API_KEY", "EMBEDDING_MODEL", "EMBEDDING_BASE_URL")  # --embedding에 필요


@dataclass(frozen=True)
class Report:
    rows: int
    row_ids: tuple[str, ...]
    recall: dict[int, float]  # k → recall@k
    by_fold: dict[str, tuple[float, int]]  # 폴드 → (recall@5, 행 수)
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
    """정답 근거가 있는 공개 튜터 행 중 교차 검증 행(홀드아웃 묶음은 뺀다, TU-16·#84)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines if line.strip()]
    return cv_rows([row for row in rows if row["labels"].get("evidence")])


def cv_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """홀드아웃 묶음에 든 행을 뺀다. 분할에 없는 행은 KeyError(조용히 섞이지 않게)."""
    split = load_splits()
    return [row for row in rows if split.cluster[row["id"]] not in split.holdout]


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
    rows = public_rows() if rows is None else cv_rows(rows)
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
        folds.setdefault(str(split.fold[split.cluster[row["id"]]]), []).append(hit)
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


class CountedEmbed:
    """임베딩 호출 수와 실패 수를 센다(TU-16 호출 수 보고, recall·run_local).

    실패: 오류·시간 초과(취소), 그리고 호출은 됐지만 받은 벡터를 쓸 수 없을 때(수가 다름·차원이
    처음과 다름·NaN·무한대·영벡터). 뒤의 것은 EmbeddingSearch가 조용히 BM25로 떨어지는 경우다.
    """

    def __init__(self, embed: Embed) -> None:
        self.embed = embed
        self.calls = 0
        self.failures = 0
        self._dim: int | None = None  # 처음 받은 벡터의 차원(기동 때 KB 묶음)

    async def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        try:
            vectors = await self.embed(texts)
        except BaseException:  # 시간 초과(취소)도 센다. 예외는 그대로 올린다
            self.failures += 1
            raise
        if not self._usable(vectors, len(texts)):
            self.failures += 1
        return vectors

    def _usable(self, vectors: Sequence[Sequence[float]], count: int) -> bool:
        if len(vectors) != count:
            return False
        for vector in vectors:
            if self._dim is None:
                self._dim = len(vector)
            norm = math.sqrt(sum(x * x for x in vector))
            if len(vector) != self._dim or not math.isfinite(norm) or norm == 0.0:
                return False
        return True


class KbEmbeddingError(RuntimeError):
    """KB 임베딩을 만들지 못했다(클래스 이름만 담는다)."""


def english_queries(rows: Sequence[dict[str, Any]]) -> int:
    """임베딩을 부르는 질의 수(retrieve와 같은 조건: 영어 위주이고 2-gram이 있다)."""
    queries = (query_parts(row["messages"])[0] for row in rows)
    return sum(1 for q in queries if is_english(q) and bigrams(q))


def kb_embedding_batches() -> int:
    """기동 때 KB 조각을 임베딩하는 호출 수(BATCH개씩)."""
    return math.ceil(len(default_index().chunks) / BATCH)


def embedding_sites(toggles: Any) -> int:
    """영어 질의 하나가 한 요청에서 임베딩을 부르는 곳 수(상한).

    `toggles.english_embedding`을 켜면 주 호출 근거 묶음 말고도 E 근거 묶음(`oom_gate`가 off가
    아닐 때)과 규칙 A 다시 찾기(`cite_top1` + `top1_dialog_query`)가 각각 질의를 임베딩한다
    (#242 리뷰: 3회 측정에서 예상 48회, 실제 106회). 끄면 주 호출 한 곳이다.
    """
    if not toggles.english_embedding:
        return 1
    rule_a = toggles.cite_top1 and toggles.top1_dialog_query
    return 1 + int(toggles.oom_gate != "off") + int(bool(rule_a))


def expected_embedding_calls(rows: Sequence[dict[str, Any]], *, runs: int, sites: int = 1) -> int:
    """임베딩을 켰을 때 운영진 임베딩 API 호출 수: 기동 때 KB 묶음 + 실행마다 영어 질의 × 부르는 곳.

    임베딩은 다시 보내지 않으므로(SDK 재시도 0, 수동 재시도 없음) 이 수가 최대값이다.
    `sites`는 `embedding_sites`(영어 장치를 켰을 때 한 요청에서 부르는 곳 수)다.
    """
    return kb_embedding_batches() + english_queries(rows) * runs * sites


def missing_embedding_env() -> list[str]:
    """운영진 임베딩 API에 필요한데 비었거나 공백뿐인 환경변수(주소가 비면 공개 OpenAI로 간다)."""
    return [name for name in EMBEDDING_ENV if not (os.environ.get(name) or "").strip()]


def _embedding(ap: argparse.ArgumentParser) -> tuple[Build, CountedEmbed]:
    """임베딩 검색을 만드는 함수를 준비한다. 부르기 전에 예상 호출 수(KB 묶음 + 영어 질의)를
    내고 상한을 본다.
    """
    missing = missing_embedding_env()
    if missing:  # 주소가 비면 SDK 기본 주소(공개 OpenAI)로 나간다
        ap.error(f"--embedding에는 {'·'.join(missing)}이 필요하다(런박스)")
    cfg = load_config()
    index = default_index()
    expected = expected_embedding_calls(public_rows(), runs=1)
    sys.stdout.write(f"임베딩 예상 호출 {expected}회(상한 {cfg.eval.max_calls}회)\n")
    if expected > cfg.eval.max_calls:
        ap.error(f"임베딩 예상 호출 {expected}회가 상한 {cfg.eval.max_calls}회를 넘는다")
    counted = CountedEmbed(openai_embed())

    async def build() -> EmbeddingSearch:
        try:
            return await EmbeddingSearch.build(
                index, counted, timeout_s=cfg.llm.timeout_s.embedding
            )
        except Exception as exc:  # 메시지는 내지 않는다
            raise KbEmbeddingError(type(exc).__name__) from None

    return build, counted


# --- rerank 측정(--rerank, exp/tutor-rerank) ---

RERANK_CONCURRENCY = 8  # 동시에 띄우는 rerank 수(런박스 동시 부하를 넘지 않게)


@dataclass(frozen=True)
class RerankRow:
    """한 행(요청)의 rerank 측정. 섹션 ID·코드·ms뿐(본문 없음, 출력은 집계만)."""

    gold: tuple[str, ...]
    bm25_top: str | None  # 근거 묶음 1위
    rule_a_top: str | None  # 규칙 A(튜터 발화까지 넣은 질의) 1위
    best: str | None  # rerank가 고른 후보(쓸 수 있을 때만)
    code: str  # called(쓸 수 있는 답) · none · failed · timeout · skipped
    ms: float
    cand_bm25: tuple[str, ...]
    cand_union: tuple[str, ...] | None  # union 후보(질의 임베딩이 됐을 때만)


@dataclass(frozen=True)
class RerankReport:
    rows: int
    bm25_hit: int
    rule_a_hit: int
    rerank_hit: int  # rerank 1위(못 쓰면 BM25 1위) = context 모드 묶음 1위
    top1_hit: int  # top1 모드 1위(못 쓰면 규칙 A 1위)
    changed_bm25: tuple[int, int, int]  # (바꾼 행, 맞게, 틀리게) — BM25 1위 대비
    changed_rule_a: tuple[int, int, int]  # 규칙 A 1위 대비
    codes: dict[str, int]
    ms: tuple[float, ...]
    cand_recall_bm25: float
    cand_recall_union: float | None  # union 후보를 잰 행만(없으면 None)
    union_rows: int


def _changes(rows: Sequence[RerankRow], base: Callable[[RerankRow], str | None]) -> tuple[int, ...]:
    moved = [r for r in rows if r.best is not None and r.best != base(r)]
    right = sum(r.best in r.gold and base(r) not in r.gold for r in moved)
    wrong = sum(base(r) in r.gold and r.best not in r.gold for r in moved)
    return len(moved), right, wrong


def summarize_rerank(rows: Sequence[RerankRow]) -> RerankReport:
    """행별 측정 → 집계(문항 본문·행 id 없음)."""
    if not rows:
        raise ValueError("측정할 행이 없다")
    codes: dict[str, int] = {}
    for r in rows:
        codes[r.code] = codes.get(r.code, 0) + 1
    unions = [r for r in rows if r.cand_union is not None]
    return RerankReport(
        rows=len(rows),
        bm25_hit=sum(r.bm25_top in r.gold for r in rows),
        rule_a_hit=sum(r.rule_a_top in r.gold for r in rows),
        rerank_hit=sum((r.best or r.bm25_top) in r.gold for r in rows),
        top1_hit=sum((r.best or r.rule_a_top) in r.gold for r in rows),
        changed_bm25=_changes(rows, lambda r: r.bm25_top),  # type: ignore[arg-type]
        changed_rule_a=_changes(rows, lambda r: r.rule_a_top),  # type: ignore[arg-type]
        codes=codes,
        ms=tuple(r.ms for r in rows if r.code != "skipped"),
        cand_recall_bm25=sum(any(g in r.cand_bm25 for g in r.gold) for r in rows) / len(rows),
        cand_recall_union=(
            sum(any(g in (r.cand_union or ()) for g in r.gold) for r in unions) / len(unions)
            if unions
            else None
        ),
        union_rows=len(unions),
    )


async def _rerank_one(
    row: dict[str, Any], cfg: Any, llm: Any, prompt: Any, emb: Any, source: str
) -> RerankRow:
    """서버 흐름과 같은 검색·후보·호출(`flow._start_rerank`·`_rule_a_top`과 같은 인자)."""
    from tutor.judge import flow, rerank
    from tutor.judge.conversation import tidy

    index = default_index()
    req = parse_request({"messages": list(row["messages"])})
    conv = tidy(flow._messages(req)) if req is not None else tidy([])
    extra: dict[str, Any] = {"index": index}
    if emb is not None:
        extra["embedding"] = emb
        if cfg.toggles.english_embedding:
            extra["english_only_embedding"] = True
    query, history = conv.search_query()
    bundle = await retrieve(query, history, k=cfg.retrieval.k, **extra)
    rule_q, rule_h = conv.search_query(all_roles=True)
    rule_a = await retrieve(rule_q, rule_h, k=cfg.retrieval.k, **extra)
    rr = cfg.llm.rerank
    union = source == "union" and emb is not None and not is_english(query)
    text = "\n".join([*history, query])[-index.max_query_chars :]
    loop = asyncio.get_running_loop()
    job = rerank.start_rerank(
        llm,
        prompt,
        conv,
        bundle,
        index,
        rr,
        deadline=loop.time() + rr.timeout_s + rr.margin_s,  # 요청 마감이 rerank 마감을 깎지 않게
        emb=emb if union else None,
        text=text,
    )
    outcome = rerank.Outcome("skipped") if job is None else await rerank.wait_rerank(job)
    return RerankRow(
        gold=tuple(row["labels"]["evidence"]),
        bm25_top=bundle[0].id if bundle else None,
        rule_a_top=rule_a[0].id if rule_a else (bundle[0].id if bundle else None),
        best=outcome.best,
        code=outcome.code,
        ms=outcome.ms,
        cand_bm25=tuple(s.id for s in bundle[: rr.candidates]),
        cand_union=outcome.candidates if outcome.union else None,
    )


async def _rerank_run(
    rows: Sequence[dict[str, Any]], cfg: Any, llm: Any, emb: Any, source: str
) -> list[RerankRow]:
    from tutor.judge.rerank import RerankPrompt

    prompt = RerankPrompt.load()
    sem = asyncio.Semaphore(RERANK_CONCURRENCY)

    async def one(row: dict[str, Any]) -> RerankRow:
        async with sem:
            return await _rerank_one(row, cfg, llm, prompt, emb, source)

    return list(await asyncio.gather(*(one(row) for row in rows)))


def measure_rerank(
    rows: Sequence[dict[str, Any]] | None = None,
    *,
    llm: Any,
    runs: int = 1,
    source: str = "bm25",
    build: Build | None = None,
    cfg: Any = None,
) -> list[RerankReport]:
    """rerank 호출만 실제로 부른다(주 호출·T·E 0회). 실행마다 집계 하나.

    rows가 없으면 공개 교차 검증 행 중 정답 근거가 있는 행(`public_rows`, 홀드아웃 제외)이다. 주면
    그대로 쓴다(테스트). build(임베딩 검색을 만드는 함수)를 주면 영어 질의는 서버처럼 임베딩
    순위이고, source가 union이면 한국어 질의도 질의 임베딩 상위를 후보에 합친다."""
    rows = public_rows() if rows is None else list(rows)
    if not rows:
        raise ValueError("측정할 행이 없다")
    cfg = cfg if cfg is not None else load_config()

    async def go() -> list[RerankReport]:
        default_index()
        emb = await build() if build is not None else None
        return [
            summarize_rerank(await _rerank_run(rows, cfg, llm, emb, source)) for _ in range(runs)
        ]

    return asyncio.run(go())


def expected_rerank_calls(rows: int, runs: int, *, union: bool, embedding: bool) -> tuple[int, int]:
    """(nano 호출, 임베딩 호출) 최대값. rerank는 재시도 0. 임베딩은 기동 때 KB 묶음 + 실행마다
    영어 질의(서버처럼 임베딩 순위, 상한으로 행 수) + union이면 한국어 질의 하나씩
    (상한으로 행 수)."""
    emb = 0
    if embedding:
        emb = kb_embedding_batches() + rows * runs * (1 + int(union))
    return rows * runs, emb


def _pct(values: Sequence[float], q: float) -> str:
    return f"{_percentile(values, q):.0f}" if values else "-"


def write_rerank(reports: Sequence[RerankReport], source: str, out: Any) -> None:
    """집계 숫자만 쓴다(문항 본문·행 id 없음)."""
    for i, r in enumerate(reports, 1):
        n = r.rows
        out.write(f"[실행 {i}/{len(reports)}] 행 {n}개 · 후보 {source}\n")
        out.write(
            f"1위 적중: BM25 {r.bm25_hit}/{n} · 규칙 A {r.rule_a_hit}/{n}"
            f" · rerank {r.rerank_hit}/{n}(못 쓰면 BM25 1위, context)"
            f" · top1 모드 {r.top1_hit}/{n}(못 쓰면 규칙 A 1위)\n"
        )
        for name, (moved, right, wrong) in (
            ("BM25 1위 대비", r.changed_bm25),
            ("규칙 A 1위 대비", r.changed_rule_a),
        ):
            out.write(f"rerank가 바꾼 행({name}) {moved} · 맞게 {right} · 틀리게 {wrong}\n")
        codes = r.codes
        out.write(
            f"none {codes.get('none', 0) / n:.1%} · 실패 {codes.get('failed', 0)}"
            f" · 시간 초과 {codes.get('timeout', 0)} · 건너뜀 {codes.get('skipped', 0)}"
            f" · 소요 ms p50 {_pct(r.ms, 0.5)} · p95 {_pct(r.ms, 0.95)}\n"
        )
        union = "-" if r.cand_recall_union is None else f"{r.cand_recall_union:.3f}"
        out.write(
            f"후보 recall: bm25 {r.cand_recall_bm25:.3f} · union {union}"
            f"(union 후보를 만든 행 {r.union_rows}개)\n"
        )


def _rerank_main(ap: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    from tutor.runtime.llm import build_llm, missing_api_env

    missing = missing_api_env()
    if missing:
        ap.error(f"--rerank에는 {'·'.join(missing)}이 필요하다(런박스)")
    if args.runs < 1:
        ap.error("--runs는 1 이상이다")
    union = args.source == "union"
    embedding = args.embedding or union
    cfg = load_config()
    rows = public_rows()
    nano, emb_calls = expected_rerank_calls(len(rows), args.runs, union=union, embedding=embedding)
    out = sys.stdout
    out.write(
        f"rerank 예상 호출: nano {nano}회 · 임베딩 {emb_calls}회(상한 {cfg.eval.max_calls}회)\n"
    )
    if nano + emb_calls > cfg.eval.max_calls:
        ap.error(f"예상 호출 {nano + emb_calls}회가 상한 {cfg.eval.max_calls}회를 넘는다")
    build, counted = _embedding(ap) if embedding else (None, None)
    llm = build_llm(types_namespace(), cfg)
    try:
        reports = measure_rerank(rows, llm=llm, runs=args.runs, source=args.source, build=build)
    except KbEmbeddingError as exc:
        ap.error(f"KB 임베딩을 만들지 못했다: {exc}")
    write_rerank(reports, args.source, out)
    out.write(f"튜터 LLM 호출 {sum(llm.calls.values())}회(역할: {', '.join(sorted(llm.calls))})\n")
    if counted is not None:
        out.write(f"임베딩 호출 {counted.calls}회(실패 {counted.failures}회)\n")
    return 0


def types_namespace() -> Any:
    """가드 LLM이 없는 state(로컬 측정): build_llm이 가드 설정의 파라미터와 자기 게이트를 쓴다."""
    import types

    return types.SimpleNamespace()


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="recall@k 측정(TU-03·TU-11)")
    ap.add_argument(
        "--embedding", action="store_true", help="영어 질의에 임베딩 RRF(운영진 임베딩 API)"
    )
    ap.add_argument(
        "--rerank",
        action="store_true",
        help="rerank 호출만 실제로 불러 1위 적중을 잰다(운영진 API, 주 호출 0회)",
    )
    ap.add_argument("--runs", type=int, default=1, help="--rerank 반복 횟수(기본 1)")
    ap.add_argument(
        "--source",
        choices=("bm25", "union"),
        default="bm25",
        help="--rerank 후보: bm25(검색 상위) | union(질의 임베딩 상위를 합침)",
    )
    args = ap.parse_args(argv)
    if args.rerank:
        return _rerank_main(ap, args)
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
