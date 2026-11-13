"""S2.9 평가 하네스 (GR-13·GR-11, master-plan §4.6). 골격: 판정기 생성 함수를 주입받는다.

서버를 띄우지 않는다. LLM 하나를 만들어 start()(probe 1회)하고, 폴드마다
factory(cfg, llm, 학습 행)로 판정기를 만들어 그 폴드 행의 judge()를 동시 16으로 부른다.
홀드아웃 묶음은 --holdout 때만 열고 기록한다.
CLI: python -m guardrail.eval.run --config configs/default.yaml --name v1 [--repeat 3] ...
"""

import argparse
import asyncio
import json
import re
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import yaml

from guardrail import guard as guard_module
from guardrail.classifier import LrClassifier
from guardrail.config import ROOT, GuardConfig, Llm, config_hash, load_config
from guardrail.data import LabeledRow, PublicRow, labeled_rows, load_public
from guardrail.eval import cv
from guardrail.eval.fake_llm import make_fake_llm
from guardrail.eval.score import as_scoring_input, score_responses, scoring
from guardrail.eval.stats import cluster_bootstrap_ci
from guardrail.guard import ConstantGuard, Judgement
from guardrail.judges.input_judge import judge_input
from guardrail.judges.output_judge import ask_grounded, output_context
from guardrail.kb import KbIndex
from guardrail.knn import KnnIndex
from guardrail.llm import LLM
from guardrail.preprocess import decode_tags, normalize, reveal_hidden, strip_tags
from guardrail.schema import Category, GuardRequest, GuardResponse, OutputVerdict
from guardrail.signals import Signals, is_low_signal

CONCURRENCY = 16
# 지금 판정 중인 행 id. Tap이 LLM 출력을 행에 묶는 데 쓴다(행마다 태스크가 달라 섞이지 않는다)
ROW_ID: ContextVar[str | None] = ContextVar("row_id", default=None)
HOLDOUT_OPEN_LIMIT = 3  # 4회째 거부(ADR-0013: 홀드아웃은 거의 보지 않는다)
RUNS_DIR = ROOT / "runs"
DEFAULT_CONFIG = "configs/default.yaml"
DEFAULT_BASELINE = "configs/baseline.yaml"
EXIT_USAGE, EXIT_OVER_BUDGET, EXIT_HOLDOUT, EXIT_BUDGET_HIT = 2, 3, 4, 5
BASELINE, DEFAULT = "baseline", "default"


class Judge(Protocol):
    async def judge(self, req: GuardRequest) -> Judgement: ...


JudgeFactory = Callable[[GuardConfig, LLM, list[LabeledRow]], Awaitable[Judge]]
LlmFactory = Callable[[Llm], LLM]


async def default_factory(cfg: GuardConfig, llm: LLM, train: list[LabeledRow]) -> Judge:
    """S2.8의 Guard.build(cfg, llm=llm, rows=train)가 들어올 자리. 그 전에는 상수 판정기다."""
    Guard = getattr(guard_module, "Guard", None)  # noqa: N806
    if Guard is None:  # S2.8 전: 안의 ImportError를 삼키지 않도록 속성 유무로만 가른다
        return ConstantGuard()
    return await Guard.build(cfg, llm=llm, rows=train)


class InputOnlyGuard:
    """입력 판정만 잰다(R14-D1): 저신호 직접 판정 + kNN few-shot + judge_input. 출력은 상수다.

    S2.8 Guard 전에 입력 프롬프트를 재는 하네스 전용 판정기다. nano가 실패하면 kNN·LR 기본값을
    쓰지 않고 normal로 두고 오류로 센다(재려는 것이 프롬프트라서다).
    """

    def __init__(self, cfg: GuardConfig, llm: LLM, train: Sequence[LabeledRow]) -> None:
        self.cfg, self.llm = cfg, llm
        self.knn = KnnIndex(train, cfg)
        self.lr = LrClassifier(train, cfg)

    async def judge(self, req: GuardRequest) -> Judgement:
        cfg = self.cfg
        raw = next((m.content for m in reversed(req.messages) if m.role == "user"), None)
        if raw is None:
            return self._result(req, "normal", "default")
        raw = raw[: cfg.input.max_chars]  # 원문 길이 상한을 전처리 전에 건다(master-plan §4.2)
        text = normalize(raw)[: cfg.retrieval.max_query_chars]
        side = {
            "knn_major": self.knn.majority(text) if cfg.toggles.knn_vote else None,
            "lr_pred": self.lr.predict(text) if cfg.toggles.lr else None,
        }
        if cfg.toggles.low_signal_direct and is_low_signal(
            strip_tags(raw), len(req.messages) > 1, decode_tags(raw)[1]
        ):
            return self._result(req, "low_signal_input", "low_signal", low=True, **side)
        examples = self.knn.fewshot(text) if cfg.toggles.knn_fewshot else []
        category = await judge_input(
            list(req.messages),
            raw,
            examples,
            self.llm,
            cfg,
            deadline=time.monotonic() + cfg.budget_s,
        )
        if category is None:
            return self._result(req, "normal", "default", calls=1, errors=("input_judge",), **side)
        return self._result(req, category, "nano", calls=1, **side)

    @staticmethod
    def _result(
        req: GuardRequest,
        category: Category,
        source: str,
        *,
        low: bool = False,
        calls: int = 0,
        errors: tuple[str, ...] = (),
        knn_major: Category | None = None,
        lr_pred: Category | None = None,
    ) -> Judgement:
        output = OutputVerdict(True, (), True) if req.has_candidate else None
        return Judgement(
            response=GuardResponse(category, output),
            input_source=source,  # type: ignore[arg-type]
            output_tier=None,
            nano_grounded=None,
            signals=Signals(low, False, False),
            knn_major=knn_major,
            lr_pred=lr_pred,
            calls=calls,
            errors=errors,
            latency_ms={},
        )


async def input_only_factory(cfg: GuardConfig, llm: LLM, train: list[LabeledRow]) -> Judge:
    return InputOnlyGuard(cfg, llm, train)


class OutputOnlyGuard:
    """출력 근거 판정만 잰다: KB 검색 → 문맥 → ask_grounded. 입력은 상수(normal)다.

    S2.8 Guard 전에 출력 프롬프트를 재는 하네스 전용 판정기다. nano의 grounded를 그대로 내고
    구간은 내지 않는다(3단·규칙 ②·safe 규칙은 S2.8). nano가 실패하면 근거 있음으로 두고 오류로 센다.
    질의와 길이 상한은 master-plan §4.2를 따른다.
    """

    def __init__(self, cfg: GuardConfig, llm: LLM) -> None:
        self.cfg, self.llm = cfg, llm
        self.kb = KbIndex.load(cfg.paths.kb, ngram=cfg.kb.ngram, k1=cfg.kb.k1, b=cfg.kb.b)

    async def judge(self, req: GuardRequest) -> Judgement:
        cfg = self.cfg
        users = [m.content[: cfg.input.max_chars] for m in req.messages if m.role == "user"]
        if not req.candidate_answer or not users:
            output = OutputVerdict(True, (), True) if req.has_candidate else None
            return self._result(output, None)
        answer = req.candidate_answer[: cfg.input.max_answer_chars]
        half = cfg.retrieval.max_query_chars // 2
        query = "\n".join(users)[-half:] + "\n" + answer[:half]  # 학습자 쪽은 최근 발화를 남긴다
        hits = output_context(self.kb.rank(query), self.kb, cfg)
        out = await ask_grounded(
            users[-1],
            reveal_hidden(answer),
            hits,
            self.llm,
            cfg,
            deadline=time.monotonic() + cfg.budget_s,
            baseline=cfg.output.mode == "baseline",
        )
        if out is None:
            return self._result(OutputVerdict(True, (), True), None, 1, ("output_judge",))
        return self._result(OutputVerdict(out.grounded, (), True), out.grounded, 1)

    @staticmethod
    def _result(
        output: OutputVerdict | None,
        nano_grounded: bool | None,
        calls: int = 0,
        errors: tuple[str, ...] = (),
    ) -> Judgement:
        return Judgement(
            response=GuardResponse("normal", output),
            input_source="default",
            output_tier=None,
            nano_grounded=nano_grounded,
            signals=Signals(False, False, False),
            knn_major=None,
            lr_pred=None,
            calls=calls,
            errors=errors,
            latency_ms={},
        )


async def output_only_factory(cfg: GuardConfig, llm: LLM, train: list[LabeledRow]) -> Judge:
    return OutputOnlyGuard(cfg, llm)


# --- 설정 -----------------------------------------------------------------


def parse_sets(items: Sequence[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items:
        key, sep, raw = item.partition("=")
        if not sep or not key:
            raise ValueError(f"--set은 키=값 형식이다: {item!r}")
        try:
            out[key] = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            raise ValueError(f"--set 값을 읽을 수 없다: {item!r}") from e
    return out


def ablation_files(directory: Path) -> dict[str, Path]:
    files = {p.stem: p for p in sorted(directory.glob("*.yaml"))}
    if not files:
        raise ValueError(f"ablation 파일이 없다: {directory}")
    return files


def select_rows(
    base: GuardConfig, args: argparse.Namespace, overrides: dict[str, Any]
) -> dict[str, GuardConfig]:
    """실행할 설정 행. 기준 행(baseline·default)은 --only와 무관하게 늘 들어간다."""
    if not args.ablation:
        return {args.name: base}
    files = ablation_files(Path(args.ablation))
    only = [x for x in (args.only or "").split(",") if x]
    unknown = [x for x in only if x not in files]
    if unknown:
        raise ValueError(f"모르는 ablation 이름: {', '.join(unknown)}")
    picked = only or list(files)
    rows = {
        BASELINE: load_config(args.baseline, overrides),
        DEFAULT: load_config(args.config, overrides),
    }
    for name in picked:
        rows[name] = load_config(args.config, overrides, ablation=files[name])
    return rows


# --- 호출 수 예측 ----------------------------------------------------------


def probe_calls(cfg: Llm) -> int:
    """LLM.probe()가 보내는 요청 수: 맨 요청 + 보낼 파라미터마다 1 + json_schema 시험 1."""
    params = sum(
        getattr(cfg, k) is not None
        for k in ("reasoning_effort", "verbosity", "max_completion_tokens")
    )
    return 1 + params + (cfg.structured_output == "json_schema")


def calls_per_row(cfg: GuardConfig, has_candidate: bool) -> int:
    """행당 호출 수의 상한 추정(저신호 직접 판정 등으로 덜 부를 수 있다). 재시도는 세지 않는다."""
    n = 1  # 입력 판정
    out = cfg.output
    if has_candidate and (
        out.mode == "baseline" or cfg.toggles.nano_tier3 or out.call_nano_when_unused
    ):
        n += 1
    if cfg.toggles.third_call:
        n += 1
    return n


def estimate_calls(
    configs: Mapping[str, GuardConfig],
    rows: Sequence[PublicRow],
    repeat: int,
    llm: Llm,
    *,
    input_only: bool = False,
    output_only: bool = False,
) -> int:
    """probe는 실행당 한 번, 나머지는 설정 행 × 반복 × 행.

    input_only면 행당 입력 1회뿐이고, output_only면 후보 답변이 있는 행만 출력 1회다.
    """
    total = 0
    for cfg in configs.values():
        if input_only:
            total += len(rows)
            continue
        if output_only:
            total += sum(bool(r.candidate_answer) for r in rows)
            continue
        total += sum(calls_per_row(cfg, r.candidate_answer is not None) for r in rows)
    return probe_calls(llm) + total * repeat


# --- 홀드아웃 열람 기록 -----------------------------------------------------

# master-plan §4.6 파일 구성의 splits/holdout_views.json(git 추적). 테스트는 monkeypatch로 바꾼다
HOLDOUT_VIEWS = cv.SPLIT_PATH.parent / "holdout_views.json"


class HoldoutRefused(Exception):
    pass


def holdout_views() -> list[dict]:
    if not HOLDOUT_VIEWS.exists():
        return []
    return json.loads(HOLDOUT_VIEWS.read_text(encoding="utf-8"))


def check_holdout_open(limit: int = HOLDOUT_OPEN_LIMIT) -> None:
    """limit회 열었으면 거부한다. 기록은 하지 않는다(start() 성공 뒤 record_holdout_open)."""
    opened = len(holdout_views())
    if opened >= limit:
        raise HoldoutRefused(f"홀드아웃은 {limit}회까지만 연다(기록 {opened}회)")


def _commit() -> str:
    try:
        r = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return r.stdout.strip()


def record_holdout_open(name: str, cfg_hash: str) -> None:
    views = holdout_views()
    views.append(
        {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "name": name,
            "commit": _commit(),
            "config_hash": cfg_hash,
        }
    )
    HOLDOUT_VIEWS.parent.mkdir(parents=True, exist_ok=True)
    HOLDOUT_VIEWS.write_text(json.dumps(views, ensure_ascii=False, indent=2) + "\n", "utf-8")


# --- 실행 -----------------------------------------------------------------


@dataclass
class Tap:
    """LLM.complete 결과를 모아 둔다(역할별 오류 코드·finish_reason=length 집계용)."""

    records: list[tuple[str, str | None, str | None]] = field(default_factory=list)
    # (행 id, 역할, 파싱된 출력). 공개 세트 분석용으로 runs/에만 쓴다. 서버 로그와 무관하다(GR-12)
    parsed: list[tuple[str | None, str, dict | None]] = field(default_factory=list)

    def attach(self, llm: LLM) -> None:
        inner = llm.complete

        async def complete(role, messages, schema, *, deadline):  # type: ignore[no-untyped-def]
            res = await inner(role, messages, schema, deadline=deadline)
            self.records.append((role, res.error, res.finish_reason))
            out = res.parsed.model_dump(mode="json") if res.parsed is not None else None
            self.parsed.append((ROW_ID.get(), role, out))
            return res

        llm.complete = complete  # type: ignore[method-assign]


def to_request(row: PublicRow) -> GuardRequest:
    return GuardRequest(row.messages, row.candidate_answer, row.candidate_answer is not None)


@dataclass
class Outcome:
    judgements: dict[str, Judgement]
    latency_ms: dict[str, float]
    judge_kind: str = ""


async def _judge_rows(judge: Judge, rows: Sequence[PublicRow], out: Outcome) -> None:
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(row: PublicRow) -> None:
        async with gate:
            ROW_ID.set(row.id)
            t0 = time.perf_counter()
            out.judgements[row.id] = await judge.judge(to_request(row))
            out.latency_ms[row.id] = (time.perf_counter() - t0) * 1000

    async with asyncio.TaskGroup() as tg:  # 하나가 실패하면 나머지를 취소한다
        for r in rows:
            tg.create_task(one(r))


async def run_once(
    cfg: GuardConfig,
    llm: LLM,
    factory: JudgeFactory,
    public: Sequence[PublicRow],
    labeled: Sequence[LabeledRow],
    split: cv.Split,
    *,
    holdout: bool,
) -> Outcome:
    """폴드마다 판정기를 만들어 그 폴드 행을 돈다. holdout이면 홀드아웃 묶음도 한 번 더 돈다."""
    out = Outcome({}, {})
    by_id = {r.id: r for r in public}
    for k in range(split.folds):
        train = cv.train_rows(split, labeled, fold=k)
        fold_ids = [i for i, c in split.cluster.items() if split.fold.get(c) == k]
        judge = await factory(cfg, llm, train)
        out.judge_kind = type(judge).__name__
        await _judge_rows(judge, [by_id[i] for i in fold_ids if i in by_id], out)
    if holdout:
        held = set(split.holdout)
        judge = await factory(cfg, llm, cv.train_rows(split, labeled, holdout=True))
        await _judge_rows(judge, [r for r in public if split.cluster[r.id] in held], out)
    return out


# --- 집계 -----------------------------------------------------------------


def _responses(out: Outcome, block: frozenset) -> dict[str, dict]:
    return {i: j.response.to_json(block) for i, j in out.judgements.items()}


def _pct(xs: Sequence[float], q: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))] if s else 0.0


def _item(r: PublicRow) -> tuple[dict, dict]:
    d = {
        "id": r.id,
        "endpoint": r.endpoint,
        "messages": [{"role": m.role, "content": m.content} for m in r.messages],
        "labels": dict(r.labels),
    }
    if r.candidate_answer is not None:
        d["candidate_answer"] = r.candidate_answer
    return as_scoring_input(d)


def row_points(rows: Sequence[PublicRow], responses: Mapping[str, dict]) -> dict[str, float]:
    """가드 행별 점수(guard_points). 설정 비교 부트스트랩의 값이다."""
    return {
        r.id: scoring.guard_points(scoring.score_guard(*_item(r), responses.get(r.id)))
        for r in rows
        if r.endpoint == "guard"
    }


def _alone(rows: Sequence[PublicRow], preds: Mapping[str, str | None], block: frozenset) -> dict:
    """kNN·LR 단독: 예측 범주를 decision으로 바꿔 block F1·범주 정확도를 낸다(없으면 오답)."""
    results, cats = [], []
    for r in rows:
        if r.endpoint != "guard":
            continue
        gold = r.labels["category"]
        pred = preds.get(r.id)
        correct = pred is not None and (pred in block) == (gold in block)
        results.append({"gold_block": gold in block, "input_correct": correct})
        if gold != "normal":
            cats.append(float(pred == gold))
    return {
        "n": len(results),
        "answered": sum(preds.get(r.id) is not None for r in rows if r.endpoint == "guard"),
        "block_f1": scoring.block_f1(results),
        "category_accuracy": sum(cats) / len(cats) if cats else 0.0,
    }


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None  # 빈 분모는 1.0이 아니라 정의 안 됨


def _prf(results: Mapping[str, dict]) -> tuple[float | None, float | None]:
    tp = sum(x["gold_block"] and x["input_correct"] for x in results.values())
    fp = sum(not x["gold_block"] and not x["input_correct"] for x in results.values())
    fn = sum(x["gold_block"] and not x["input_correct"] for x in results.values())
    return _ratio(tp, tp + fp), _ratio(tp, tp + fn)


def _tier_hit(cv_rows, out: Outcome, results: Mapping[str, dict]) -> dict:
    tiers: dict[str, list[float]] = {}
    for r in cv_rows:
        j = out.judgements[r.id]
        if r.endpoint == "guard" and j.output_tier is not None and results[r.id]["has_output"]:
            hit = float(bool(results[r.id]["output_correct"]))
            tiers.setdefault(str(j.output_tier), []).append(hit)
    return {t: {"n": len(v), "hit": sum(v) / len(v)} for t, v in sorted(tiers.items())}


def _llm_by_role(tap: Sequence[tuple[str, str | None, str | None]]) -> dict:
    by_role: dict[str, dict[str, Any]] = {}
    for role, error, finish in tap:
        d = by_role.setdefault(role, {"calls": 0, "errors": Counter(), "length": 0})
        d["calls"] += 1
        d["errors"][error or "ok"] += 1
        d["length"] += finish == "length"
    return {
        k: {
            "calls": v["calls"],
            "error_rate": {c: n / v["calls"] for c, n in v["errors"].items() if c != "ok"},
            "finish_length_rate": v["length"] / v["calls"],
        }
        for k, v in by_role.items()
    }


def summarize(
    rows: Sequence[PublicRow],
    out: Outcome,
    tap: Sequence[tuple[str, str | None, str | None]],
    cfg: GuardConfig,
    split: cv.Split,
) -> dict:
    block = frozenset(cfg.block_categories)
    held = set(split.holdout)
    cv_rows = [r for r in rows if split.cluster[r.id] not in held and r.id in out.judgements]
    ho_rows = [r for r in rows if split.cluster[r.id] in held and r.id in out.judgements]
    resp = _responses(out, block)
    sizes = Counter(split.cluster[r.id] for r in rows if r.endpoint == "guard")
    results = {
        r.id: scoring.score_guard(*_item(r), resp.get(r.id))
        for r in cv_rows
        if r.endpoint == "guard"
    }
    pairs = [x["input_correct"] for i, x in results.items() if sizes[split.cluster[i]] > 1]
    confusion = Counter(
        f"{r.labels['category']}>{(resp[r.id].get('input') or {}).get('category')}"
        for r in cv_rows
        if r.endpoint == "guard"
    )
    precision, recall = _prf(results)
    lat = list(out.latency_ms.values())
    return {
        "cv": score_responses(cv_rows, resp, block),
        "holdout": score_responses(ho_rows, resp, block) if ho_rows else None,
        "precision": precision,
        "recall": recall,
        "pair_block_accuracy": {"n": len(pairs), "value": _ratio(sum(pairs), len(pairs))},
        "confusion": dict(confusion),
        "tier_hit": _tier_hit(cv_rows, out, results),
        "knn_alone": _alone(cv_rows, {i: j.knn_major for i, j in out.judgements.items()}, block),
        "lr_alone": _alone(cv_rows, {i: j.lr_pred for i, j in out.judgements.items()}, block),
        "calls": {
            "total": sum(j.calls for j in out.judgements.values()),
            "requests": len(out.judgements),
        },
        "errors": dict(Counter(e for j in out.judgements.values() for e in j.errors)),
        "llm_by_role": _llm_by_role(tap),
        "latency_ms": {"p50": _pct(lat, 0.5), "p95": _pct(lat, 0.95)},
    }


# --- 보고서 ---------------------------------------------------------------

NA = "n/a"


def _range(xs: Sequence[float | None]) -> str:
    vals = [x for x in xs if x is not None]
    if not vals:
        return NA
    lo, hi = min(vals), max(vals)
    return f"{lo:.2f}" if lo == hi else f"{lo:.2f}–{hi:.2f}"


def _f(x: float | None, spec: str = ".3f") -> str:
    return NA if x is None else format(x, spec)


def _overlaps(a: Sequence[float], b: Sequence[float]) -> bool:
    return max(a) >= min(b) and max(b) >= min(a)


def add_variance(summary: dict) -> None:
    """채택 기준은 default의 반복 범위 밖(PRD §7): 행마다 default 범위와 겹치는지 적는다."""
    cfgs = summary["configs"]
    rng = {c: [s["cv"]["guard_score"] for s in v["scores"]] for c, v in cfgs.items()}
    summary["variance"] = {
        "measured": summary["repeat"] >= 3,
        "overlaps_default": {
            c: _overlaps(r, rng[DEFAULT]) for c, r in rng.items() if DEFAULT in rng and c != DEFAULT
        },
    }


def _banner(summary: dict) -> list[str]:
    lines = []
    if summary.get("fake") or summary.get("judge_kind") == "ConstantGuard":
        lines.append("# FAKE/상수 판정기 — 점수 무효 (results/로 옮기지 않는다)")
    if summary.get("output_only"):
        lines.append(
            "# 출력 단독 실행 — 입력은 상수(normal)라 가드 점수·block F1은 비교에 쓰지 않는다"
        )
    if summary.get("input_only"):
        lines.append("# 입력 단독 실행 — 출력·가드 점수는 상수 출력 기준이라 비교에 쓰지 않는다")
    if summary.get("invalid"):
        lines.append(f"# 무효 실행 — {summary['invalid']}")
    return lines


def _table(summary: dict) -> list[str]:
    var = summary.get("variance", {})
    head = [
        "| 설정 | 가드 점수 | block F1 | 정밀도 | 재현율 | 범주 정확도 | 짝 block 정확도 "
        "| default 범위와 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    rows = []
    for name, c in summary["configs"].items():
        s = c["scores"]
        pair = s[0]["pair_block_accuracy"]
        ov = var.get("overlaps_default", {}).get(name)
        rel = "—" if ov is None else ("겹침(개선 아님)" if ov else "밖")
        rows.append(
            f"| {name} | {_range([x['cv']['guard_score'] for x in s])}"
            f" | {_range([x['cv']['block_f1'] for x in s])}"
            f" | {_range([x['precision'] for x in s])}"
            f" | {_range([x['recall'] for x in s])}"
            f" | {_range([x['cv']['category_accuracy'] for x in s])}"
            f" | {_range([x['pair_block_accuracy']['value'] for x in s])} (n={pair['n']})"
            f" | {rel} |"
        )
    return head + rows


def _section(name: str, s: dict) -> list[str]:
    o = s["cv"]["output"]
    t = s["cv"]["tutor"]
    k = s["calls"]
    tiers = ", ".join(f"{n}단 {v['hit']:.2f} (n={v['n']})" for n, v in s["tier_hit"].items())
    errors = ", ".join(f"{e} {n}" for e, n in sorted(s["errors"].items())) or "없음"
    out = ["", f"## {name}", ""]
    out.append(
        f"- 출력 평균 {o['mean']:.3f} (grounded {o['grounded']:.3f}, 구간 F1 "
        f"{o['span_f1']:.3f}, safe {o['safe']:.3f}, n={o['n']})"
    )
    out.append(f"- 단별(①–④) 적중: {tiers or '없음'}")
    for label, key in (("kNN", "knn_alone"), ("LR", "lr_alone")):
        a = s[key]
        out.append(
            f"- {label} 단독: block F1 {a['block_f1']:.3f}, 범주 {a['category_accuracy']:.3f} "
            f"(응답 {a['answered']}/{a['n']})"
        )
    out.append(
        f"- 튜터 {t['n']}행(따로): 범주 정확도 {t['category_accuracy']:.3f}, "
        f"block F1 {t['block_f1']:.3f}"
    )
    out.append(f"- 호출 {k['total']}회, 요청당 {k['total'] / max(1, k['requests']):.2f}회")
    out.append(f"- 지연 p50 {s['latency_ms']['p50']:.0f}ms, p95 {s['latency_ms']['p95']:.0f}ms")
    out.append(f"- 판정기 오류 코드: {errors}")
    for role, d in s["llm_by_role"].items():
        errs = ", ".join(f"{e} {r:.1%}" for e, r in d["error_rate"].items()) or "없음"
        out.append(
            f"- {role}: 호출 {d['calls']}, 오류 {errs}, "
            f"finish_reason=length {d['finish_length_rate']:.1%}"
        )
    out.append(
        "- 혼동행렬(정답>예측: 수): "
        + ", ".join(f"{k}:{n}" for k, n in sorted(s["confusion"].items()))
    )
    if s["holdout"]:
        out.append(f"- 홀드아웃 가드 점수 {s['holdout']['guard_score']:.2f} (열람 기록 있음)")
    return out


def _compare_table(compare: Mapping[str, dict]) -> list[str]:
    lines = [
        "",
        "## default 대비 행별 점수 평균 차이",
        "",
        "guard_points 평균의 차이(가드 점수의 대용치). 묶음 단위 부트스트랩 95%, "
        "다중 비교 보정 없음, 채택 기준 아님.",
        "",
        "| 설정 | 평균 차이 | 95% 구간 |",
        "|---|---|---|",
    ]
    for name, d in compare.items():
        lines.append(f"| {name} | {d['mean']:+.4f} | [{d['low']:+.4f}, {d['high']:+.4f}] |")
    return lines


def render_report(name: str, summary: dict) -> str:
    lines = _banner(summary)
    lines += [f"# 평가 보고서: {name}", ""]
    lines.append(
        f"- 반복 {summary['repeat']}회, 설정 {len(summary['configs'])}행, "
        f"판정기 {summary.get('judge_kind')}, fake={summary.get('fake')}"
    )
    if not summary.get("variance", {}).get("measured"):
        lines.append("- **분산 미측정**(--repeat < 3): 설정 간 차이를 개선으로 읽지 않는다")
    lines += ["", "## 가드 점수(교차 검증, 반복 범위)", "", *_table(summary)]
    for cname, c in summary["configs"].items():
        lines += _section(cname, c["scores"][0])
    if summary.get("compare"):
        lines += _compare_table(summary["compare"])
    return "\n".join(lines) + "\n"


# --- CLI ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="guardrail.eval.run", description="가드 평가 하네스")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--baseline", default=DEFAULT_BASELINE, help="--ablation의 기준 행 설정")
    ap.add_argument("--name", required=True)
    ap.add_argument("--set", action="append", default=[], metavar="키=값", dest="sets")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--max-calls", type=int, default=None)
    ap.add_argument("--estimate", action="store_true", help="예상 호출 수만 찍고 끝낸다(호출 0)")
    ap.add_argument("--fake", action="store_true", help="고정 응답 LLM(API 0회, 점수 무효)")
    ap.add_argument("--ablation", metavar="DIR", help="토글별 yaml 폴더")
    ap.add_argument("--only", help="--ablation 중 이 이름만(쉼표). baseline·default는 늘 포함")
    ap.add_argument("--holdout", action="store_true", help="홀드아웃도 잰다(열람 기록, 4회째 거부)")
    ap.add_argument(
        "--input-only", action="store_true", help="입력 판정만 잰다(출력은 상수, 행당 호출 ≤1)"
    )
    ap.add_argument(
        "--output-only", action="store_true", help="출력 근거 판정만 잰다(입력은 상수 normal)"
    )
    ap.add_argument("--out", default=str(RUNS_DIR))
    return ap


def _write(out_dir: Path, responses: list[dict], summary: dict, report: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "responses.jsonl").open("w", encoding="utf-8") as f:
        for rec in responses:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "report.md").write_text(report, encoding="utf-8")


def _say(text: str) -> None:
    sys.stdout.write(text + "\n")  # CLI 출력(T20은 print만 막는다)


def _err(text: str) -> None:
    sys.stderr.write(f"error: {text}\n")


@dataclass
class Plan:
    base: GuardConfig
    configs: dict[str, GuardConfig]
    public: list[PublicRow]
    labeled: list[LabeledRow]
    split: cv.Split
    estimate: int
    limit: int


def _usage_error(args: argparse.Namespace) -> str | None:
    if args.repeat < 1:
        return "--repeat은 1 이상"
    if args.input_only and args.output_only:
        return "--input-only와 --output-only는 함께 쓰지 않는다"
    if args.holdout and args.fake:
        return (
            "--fake와 --holdout은 함께 쓰지 않는다(가짜 응답으로 홀드아웃 열람 횟수를 쓰지 않는다)"
        )
    if args.holdout and (args.ablation or args.repeat > 1):
        return "--holdout은 단일 설정·--repeat 1에서만 쓴다(설정 비교에 홀드아웃을 쓰지 않는다)"
    if not re.fullmatch(r"[\w.-]+", args.name) or args.name in {".", ".."}:
        return "--name은 영문·숫자·_.-만 쓴다"
    return None


def _make_plan(args: argparse.Namespace) -> Plan:
    overrides = parse_sets(args.sets)
    base = load_config(args.config, overrides)
    configs = select_rows(base, args, overrides)
    public = load_public(base.paths.public)
    split = cv.load_splits()
    if missing := {r.id for r in public} - split.cluster.keys():
        raise ValueError(f"분할 파일에 없는 행 {len(missing)}개")
    held = set(split.holdout)
    judged = [r for r in public if args.holdout or split.cluster[r.id] not in held]
    limit = args.max_calls if args.max_calls is not None else base.eval.max_calls
    est = estimate_calls(
        configs,
        judged,
        args.repeat,
        base.llm,
        input_only=args.input_only,
        output_only=args.output_only,
    )
    _say(
        f"estimate_calls={est} probe={probe_calls(base.llm)} max_calls={limit} "
        f"configs={','.join(configs)} judged_rows={len(judged)} repeat={args.repeat}"
    )
    return Plan(base, configs, public, labeled_rows(public), split, est, limit)


async def _measure(
    plan: Plan, args: argparse.Namespace, llm: LLM, tap: Tap, factory: JudgeFactory
) -> tuple[dict, list[dict]]:
    held = set(plan.split.holdout)
    summary: dict[str, Any] = {"name": args.name, "repeat": args.repeat, "configs": {}}
    records: list[dict] = []
    points: dict[str, dict[str, list[float]]] = {}
    for cname, cfg in plan.configs.items():
        block = frozenset(cfg.block_categories)
        scores = []
        for rep in range(args.repeat):
            mark, pmark = len(tap.records), len(tap.parsed)
            out = await run_once(
                cfg, llm, factory, plan.public, plan.labeled, plan.split, holdout=args.holdout
            )
            summary["judge_kind"] = out.judge_kind
            scores.append(summarize(plan.public, out, tap.records[mark:], cfg, plan.split))
            resp = _responses(out, block)
            llm_out: dict[str, list[dict]] = {}
            for rid, role, parsed in tap.parsed[pmark:]:
                if rid is not None:
                    llm_out.setdefault(rid, []).append({"role": role, "parsed": parsed})
            for rid, p in row_points([r for r in plan.public if r.id in resp], resp).items():
                if plan.split.cluster[rid] not in held:
                    points.setdefault(cname, {}).setdefault(rid, []).append(p)
            records += [
                {
                    "config": cname,
                    "repeat": rep,
                    "id": rid,
                    "response": resp[rid],
                    "source": j.input_source,
                    "tier": j.output_tier,
                    "answer_quote_ok": j.answer_quote_ok,
                    "kb_quote_ok": j.kb_quote_ok,
                    "nano_refuses": j.nano_refuses,
                    "calls": j.calls,
                    "errors": list(j.errors),
                    "knn_major": j.knn_major,
                    "lr_pred": j.lr_pred,
                    "llm": llm_out.get(rid, []),
                }
                for rid, j in out.judgements.items()
            ]
        summary["configs"][cname] = {"config_hash": config_hash(cfg), "scores": scores}
    add_variance(summary)
    if len(points) > 1 and DEFAULT in points:
        summary["compare"] = _compare(points, plan.split)
    return summary, records


async def amain(
    argv: Sequence[str] | None = None,
    *,
    make_llm: LlmFactory | None = None,
    judge_factory: JudgeFactory | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if msg := _usage_error(args):
        _err(msg)
        return EXIT_USAGE
    try:
        plan = _make_plan(args)
    except (ValueError, OSError) as e:
        _err(f"{type(e).__name__}: {e}")
        return EXIT_USAGE
    if args.estimate:
        return 0
    if plan.estimate > plan.limit:
        _err(f"예상 호출 {plan.estimate}회가 상한 {plan.limit}회를 넘는다. 시작하지 않는다")
        return EXIT_OVER_BUDGET
    try:
        if args.holdout:
            check_holdout_open()  # start() 전에 거부한다
    except HoldoutRefused as e:
        _err(str(e))
        return EXIT_HOLDOUT
    only = (args.input_only and input_only_factory) or (args.output_only and output_only_factory)
    factory = judge_factory or only or default_factory
    if factory is default_factory and not hasattr(guard_module, "Guard"):
        sys.stderr.write("note: guardrail.guard.Guard가 아직 없어 ConstantGuard로 돈다\n")
    llm = make_fake_llm(plan.base.llm) if args.fake else (make_llm or LLM)(plan.base.llm)
    tap = Tap()
    tap.attach(llm)
    llm.max_calls = plan.limit - probe_calls(plan.base.llm)  # probe는 complete()를 거치지 않는다
    try:
        await llm.start()  # probe는 실행당 한 번
    except ValueError as e:  # 환경변수 없음·인증 거절
        _err(str(e))
        return EXIT_USAGE
    if args.holdout:  # 열람은 start()가 성공한 뒤에만 센다
        record_holdout_open(args.name, config_hash(plan.base))

    summary, records = await _measure(plan, args, llm, tap, factory)
    summary["fake"] = args.fake
    summary["input_only"] = args.input_only
    summary["output_only"] = args.output_only
    spent = sum(e == "budget" for _, e, _ in tap.records)
    if spent:
        summary["invalid"] = f"budget 오류 {spent}회: 결과를 비교에 쓰지 않는다"
    out_dir = Path(args.out) / args.name
    _write(out_dir, records, summary, render_report(args.name, summary))
    _say(f"wrote {out_dir}")
    if spent:
        _err(f"호출 상한에 걸려 budget 오류 {spent}회. 결과는 무효다")
        return EXIT_BUDGET_HIT
    return 0


def _compare(points: Mapping[str, Mapping[str, list[float]]], split: cv.Split) -> dict:
    mean = {c: {i: sum(v) / len(v) for i, v in rows.items()} for c, rows in points.items()}
    out = {}
    for cname, rows in mean.items():
        if cname == DEFAULT:
            continue
        diff = {i: rows[i] - mean[DEFAULT][i] for i in rows if i in mean[DEFAULT]}
        if not diff:
            continue
        lo, hi = cluster_bootstrap_ci(diff, split.cluster)
        out[cname] = {"mean": sum(diff.values()) / len(diff), "low": lo, "high": hi}
    return out


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(amain(argv))


if __name__ == "__main__":
    raise SystemExit(main())
