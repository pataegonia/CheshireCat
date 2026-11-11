# ruff: noqa: E501  (보고서 서식 문자열은 한 줄이 읽기 쉽다)
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
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import yaml

from guardrail import guard as guard_module
from guardrail.config import ROOT, GuardConfig, Llm, config_hash, load_config
from guardrail.data import LabeledRow, PublicRow, labeled_rows, load_public
from guardrail.eval import cv
from guardrail.eval.fake_llm import make_fake_llm
from guardrail.eval.score import as_scoring_input, score_responses, scoring
from guardrail.eval.stats import cluster_bootstrap_ci
from guardrail.guard import ConstantGuard, Judgement
from guardrail.llm import LLM
from guardrail.schema import GuardRequest

CONCURRENCY = 16
HOLDOUT_OPEN_LIMIT = 3  # 4회째 거부(ADR-0013: 홀드아웃은 거의 보지 않는다)
HOLDOUT_LOG = ROOT / "results" / "holdout-log.jsonl"
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
    configs: Mapping[str, GuardConfig], rows: Sequence[PublicRow], repeat: int, llm: Llm
) -> int:
    """probe는 실행당 한 번, 나머지는 설정 행 × 반복 × 행."""
    total = 0
    for cfg in configs.values():
        total += sum(calls_per_row(cfg, r.candidate_answer is not None) for r in rows)
    return probe_calls(llm) + total * repeat


# --- 홀드아웃 열람 기록 -----------------------------------------------------


class HoldoutRefused(Exception):
    pass


def open_holdout(log: Path, name: str, cfg_hash: str, limit: int = HOLDOUT_OPEN_LIMIT) -> None:
    """열람 횟수를 확인하고 한 줄 남긴다. limit회 열었으면 거부한다."""
    opened = log.read_text(encoding="utf-8").count("\n") if log.exists() else 0
    if opened >= limit:
        raise HoldoutRefused(f"홀드아웃은 {limit}회까지만 연다(기록 {opened}회: {log})")
    log.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "name": name,
        "config": cfg_hash,
    }
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# --- 실행 -----------------------------------------------------------------


@dataclass
class Tap:
    """LLM.complete 결과를 모아 둔다(역할별 오류 코드·finish_reason=length 집계용)."""

    records: list[tuple[str, str | None, str | None]] = field(default_factory=list)

    def attach(self, llm: LLM) -> None:
        inner = llm.complete

        async def complete(role, messages, schema, *, deadline):  # type: ignore[no-untyped-def]
            res = await inner(role, messages, schema, deadline=deadline)
            self.records.append((role, res.error, res.finish_reason))
            return res

        llm.complete = complete  # type: ignore[method-assign]


def to_request(row: PublicRow) -> GuardRequest:
    return GuardRequest(row.messages, row.candidate_answer, row.candidate_answer is not None)


@dataclass
class Outcome:
    judgements: dict[str, Judgement]
    latency_ms: dict[str, float]


async def _judge_rows(judge: Judge, rows: Sequence[PublicRow], out: Outcome) -> None:
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(row: PublicRow) -> None:
        async with gate:
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


def row_points(rows: Sequence[PublicRow], responses: Mapping[str, dict]) -> dict[str, float]:
    """가드 행별 점수(guard_points). 설정 비교 부트스트랩의 값이다."""
    out = {}
    for r in rows:
        if r.endpoint != "guard":
            continue
        d = {
            "id": r.id,
            "endpoint": r.endpoint,
            "messages": [{"role": m.role, "content": m.content} for m in r.messages],
            "labels": dict(r.labels),
        }
        if r.candidate_answer is not None:
            d["candidate_answer"] = r.candidate_answer
        item, variant = as_scoring_input(d)
        out[r.id] = scoring.guard_points(scoring.score_guard(item, variant, responses.get(r.id)))
    return out


def _alone(rows: Sequence[PublicRow], preds: Mapping[str, str | None], block: frozenset) -> dict:
    """kNN·LR 단독: 예측 범주를 decision으로 바꿔 block F1·범주 정확도를 낸다(없으면 오답)."""
    results, cats = [], []
    for r in rows:
        if r.endpoint != "guard":
            continue
        gold = r.labels["category"]
        pred = preds.get(r.id)
        gold_block = gold in block
        results.append(
            {
                "gold_block": gold_block,
                "input_correct": pred is not None and (pred in block) == gold_block,
            }
        )
        if gold != "normal":
            cats.append(float(pred == gold))
    return {
        "n": len(results),
        "answered": sum(preds.get(r.id) is not None for r in rows if r.endpoint == "guard"),
        "block_f1": scoring.block_f1(results),
        "category_accuracy": sum(cats) / len(cats) if cats else 0.0,
    }


def summarize(
    rows: Sequence[PublicRow], out: Outcome, tap: Sequence[tuple[str, str | None, str | None]],
    cfg: GuardConfig, split: cv.Split,
) -> dict:  # fmt: skip
    block = frozenset(cfg.block_categories)
    held = set(split.holdout)
    cv_rows = [r for r in rows if split.cluster[r.id] not in held and r.id in out.judgements]
    ho_rows = [r for r in rows if split.cluster[r.id] in held and r.id in out.judgements]
    resp = _responses(out, block)
    sizes = Counter(split.cluster[r.id] for r in rows if r.endpoint == "guard")
    results = {
        r.id: scoring.score_guard(*as_scoring_input_for(r), resp.get(r.id))
        for r in cv_rows
        if r.endpoint == "guard"
    }
    tp = sum(x["gold_block"] and x["input_correct"] for x in results.values())
    fp = sum(not x["gold_block"] and not x["input_correct"] for x in results.values())
    fn = sum(x["gold_block"] and not x["input_correct"] for x in results.values())
    pairs = [x["input_correct"] for i, x in results.items() if sizes[split.cluster[i]] > 1]
    confusion: Counter[str] = Counter()
    for r in cv_rows:
        if r.endpoint == "guard":
            pred = (resp[r.id].get("input") or {}).get("category")
            confusion[f"{r.labels['category']}>{pred}"] += 1
    tiers: dict[str, list[float]] = {}
    for r in cv_rows:
        j = out.judgements[r.id]
        if r.endpoint == "guard" and j.output_tier is not None and results[r.id]["has_output"]:
            tiers.setdefault(str(j.output_tier), []).append(
                float(bool(results[r.id]["output_correct"]))
            )
    n_req = len(out.judgements)
    errors = Counter(e for j in out.judgements.values() for e in j.errors)
    by_role: dict[str, dict[str, Any]] = {}
    for role, error, finish in tap:
        d = by_role.setdefault(role, {"calls": 0, "errors": Counter(), "length": 0})
        d["calls"] += 1
        d["errors"][error or "ok"] += 1
        d["length"] += finish == "length"
    lat = list(out.latency_ms.values())
    return {
        "cv": score_responses(cv_rows, resp, block),
        "holdout": score_responses(ho_rows, resp, block) if ho_rows else None,
        "precision": tp / (tp + fp) if tp + fp else 1.0,
        "recall": tp / (tp + fn) if tp + fn else 1.0,
        "pair_block_accuracy": {
            "n": len(pairs),
            "value": sum(pairs) / len(pairs) if pairs else 0.0,
        },
        "confusion": dict(confusion),
        "tier_hit": {t: {"n": len(v), "hit": sum(v) / len(v)} for t, v in sorted(tiers.items())},
        "knn_alone": _alone(cv_rows, {i: j.knn_major for i, j in out.judgements.items()}, block),
        "lr_alone": _alone(cv_rows, {i: j.lr_pred for i, j in out.judgements.items()}, block),
        "calls": {"total": sum(j.calls for j in out.judgements.values()), "requests": n_req},
        "errors": dict(errors),
        "llm_by_role": {
            k: {
                "calls": v["calls"],
                "error_rate": {c: n / v["calls"] for c, n in v["errors"].items() if c != "ok"},
                "finish_length_rate": v["length"] / v["calls"],
            }
            for k, v in by_role.items()
        },
        "latency_ms": {"p50": _pct(lat, 0.5), "p95": _pct(lat, 0.95)},
    }


def as_scoring_input_for(r: PublicRow) -> tuple[dict, dict]:
    d = {
        "id": r.id,
        "endpoint": r.endpoint,
        "messages": [{"role": m.role, "content": m.content} for m in r.messages],
        "labels": dict(r.labels),
    }
    if r.candidate_answer is not None:
        d["candidate_answer"] = r.candidate_answer
    return as_scoring_input(d)


def _range(xs: Sequence[float]) -> str:
    return f"{min(xs):.2f}" if min(xs) == max(xs) else f"{min(xs):.2f}–{max(xs):.2f}"


def render_report(name: str, summary: dict) -> str:
    L = [
        f"# 평가 보고서: {name}",
        "",
        f"- 반복 {summary['repeat']}회, 설정 {len(summary['configs'])}행",
        "",
    ]
    L += [
        "## 가드 점수(교차 검증, 반복 범위)",
        "",
        "| 설정 | 가드 점수 | block F1 | 정밀도 | 재현율 | 범주 정확도 | 짝 block 정확도 |",
        "|---|---|---|---|---|---|---|",
    ]
    for cname, c in summary["configs"].items():
        s = c["scores"]
        L.append(
            f"| {cname} | {_range([x['cv']['guard_score'] for x in s])} | {_range([x['cv']['block_f1'] for x in s])}"
            f" | {_range([x['precision'] for x in s])} | {_range([x['recall'] for x in s])}"
            f" | {_range([x['cv']['category_accuracy'] for x in s])}"
            f" | {_range([x['pair_block_accuracy']['value'] for x in s])} (n={s[0]['pair_block_accuracy']['n']}) |"
        )
    for cname, c in summary["configs"].items():
        s = c["scores"][0]
        L += ["", f"## {cname}", ""]
        o = s["cv"]["output"]
        L.append(
            f"- 출력 평균 {o['mean']:.3f} (grounded {o['grounded']:.3f}, 구간 F1 {o['span_f1']:.3f}, safe {o['safe']:.3f}, n={o['n']})"
        )
        L.append(
            "- 단별(①–④) 적중: "
            + (
                ", ".join(f"{t}단 {v['hit']:.2f} (n={v['n']})" for t, v in s["tier_hit"].items())
                or "없음"
            )
        )
        L.append(
            f"- kNN 단독: block F1 {s['knn_alone']['block_f1']:.3f}, 범주 {s['knn_alone']['category_accuracy']:.3f} (응답 {s['knn_alone']['answered']}/{s['knn_alone']['n']})"
        )
        L.append(
            f"- LR 단독: block F1 {s['lr_alone']['block_f1']:.3f}, 범주 {s['lr_alone']['category_accuracy']:.3f} (응답 {s['lr_alone']['answered']}/{s['lr_alone']['n']})"
        )
        t = s["cv"]["tutor"]
        L.append(
            f"- 튜터 {t['n']}행(따로): 범주 정확도 {t['category_accuracy']:.3f}, block F1 {t['block_f1']:.3f}"
        )
        k = s["calls"]
        L.append(f"- 호출 {k['total']}회, 요청당 {k['total'] / max(1, k['requests']):.2f}회")
        L.append(f"- 지연 p50 {s['latency_ms']['p50']:.0f}ms, p95 {s['latency_ms']['p95']:.0f}ms")
        L.append(
            "- 판정기 오류 코드: "
            + (", ".join(f"{e} {n}" for e, n in sorted(s["errors"].items())) or "없음")
        )
        for role, d in s["llm_by_role"].items():
            errs = ", ".join(f"{e} {r:.1%}" for e, r in d["error_rate"].items()) or "없음"
            L.append(
                f"- {role}: 호출 {d['calls']}, 오류 {errs}, finish_reason=length {d['finish_length_rate']:.1%}"
            )
        L.append(
            "- 혼동행렬(정답>예측: 수): "
            + ", ".join(f"{k}:{n}" for k, n in sorted(s["confusion"].items()))
        )
        if s["holdout"]:
            L.append(f"- 홀드아웃 가드 점수 {s['holdout']['guard_score']:.2f} (열람 기록 있음)")
    cmp_ = summary.get("compare")
    if cmp_:
        L += [
            "",
            "## default 대비 행별 점수 평균 차이(guard_points 평균, 가드 점수의 대용치. 묶음 단위 부트스트랩 95%, 다중 비교 보정 없음, 채택 기준 아님)",
            "",
            "| 설정 | 평균 차이 | 95% 구간 |",
            "|---|---|---|",
        ]
        for cname, d in cmp_.items():
            L.append(f"| {cname} | {d['mean']:+.4f} | [{d['low']:+.4f}, {d['high']:+.4f}] |")
    return "\n".join(L) + "\n"


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
    ap.add_argument("--fake", action="store_true", help="고정 응답 LLM(API 0회)")
    ap.add_argument("--ablation", metavar="DIR", help="토글별 yaml 폴더")
    ap.add_argument("--only", help="--ablation 중 이 이름만(쉼표). baseline·default는 늘 포함")
    ap.add_argument("--holdout", action="store_true", help="홀드아웃도 잰다(열람 기록, 4회째 거부)")
    ap.add_argument("--out", default=str(RUNS_DIR))
    ap.add_argument("--holdout-log", default=str(HOLDOUT_LOG))
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


async def amain(
    argv: Sequence[str] | None = None,
    *,
    make_llm: LlmFactory | None = None,
    judge_factory: JudgeFactory | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        overrides = parse_sets(args.sets)
        base = load_config(args.config, overrides)
        configs = select_rows(base, args, overrides)
    except (ValueError, OSError) as e:
        sys.stderr.write(f"error: {type(e).__name__}: {e}\n")
        return EXIT_USAGE
    if args.holdout and (args.ablation or args.repeat > 1):
        sys.stderr.write(
            "error: --holdout은 단일 설정·--repeat 1에서만 쓴다(여러 설정 비교에 홀드아웃을 쓰지 않는다)\n"
        )
        return EXIT_USAGE
    if args.repeat < 1:
        sys.stderr.write("error: --repeat은 1 이상\n")
        return EXIT_USAGE

    if not re.fullmatch(r"[\w.-]+", args.name) or args.name in {".", ".."}:
        sys.stderr.write("error: --name은 영문·숫자·_.-만 쓴다\n")
        return EXIT_USAGE
    public = load_public(base.paths.public)
    labeled = labeled_rows(public)
    split = cv.load_splits()
    if missing := {r.id for r in public} - split.cluster.keys():
        sys.stderr.write(
            f"error: 분할 파일에 없는 행 {len(missing)}개. cv --write를 다시 확인한다\n"
        )
        return EXIT_USAGE
    held = set(split.holdout)
    judged = [r for r in public if args.holdout or split.cluster[r.id] not in held]

    est = estimate_calls(configs, judged, args.repeat, base.llm)
    limit = args.max_calls if args.max_calls is not None else base.eval.max_calls
    _say(
        f"estimate_calls={est} probe={probe_calls(base.llm)} max_calls={limit} "
        f"rows={','.join(configs)} judged_rows={len(judged)} repeat={args.repeat}"
    )
    if args.estimate:
        return 0
    if est > limit:
        sys.stderr.write(f"error: 예상 호출 {est}회가 상한 {limit}회를 넘는다. 시작하지 않는다\n")
        return EXIT_OVER_BUDGET
    if judge_factory is None and not hasattr(guard_module, "Guard"):
        sys.stderr.write("note: guardrail.guard.Guard가 아직 없어 ConstantGuard로 돈다\n")
    if args.fake:
        llm = make_fake_llm(base.llm)
    else:
        llm = (make_llm or LLM)(base.llm)
    tap = Tap()
    tap.attach(llm)
    llm.max_calls = limit - probe_calls(base.llm)  # probe는 complete()를 거치지 않는다
    try:
        await llm.start()  # probe는 실행당 한 번
    except ValueError as e:  # 환경변수 없음·인증 거절
        sys.stderr.write(f"error: {e}\n")
        return EXIT_USAGE
    if args.holdout:  # 열람은 start()가 성공한 뒤에만 센다
        try:
            open_holdout(Path(args.holdout_log), args.name, config_hash(base))
        except HoldoutRefused as e:
            sys.stderr.write(f"error: {e}\n")
            return EXIT_HOLDOUT

    factory = judge_factory or default_factory
    summary: dict[str, Any] = {"name": args.name, "repeat": args.repeat, "configs": {}}
    records: list[dict] = []
    points: dict[str, dict[str, list[float]]] = {}
    for cname, cfg in configs.items():
        block = frozenset(cfg.block_categories)
        scores = []
        for rep in range(args.repeat):
            mark = len(tap.records)
            out = await run_once(cfg, llm, factory, public, labeled, split, holdout=args.holdout)
            scores.append(summarize(public, out, tap.records[mark:], cfg, split))
            resp = _responses(out, block)
            for rid, p in row_points([r for r in public if r.id in resp], resp).items():
                if split.cluster[rid] not in held:
                    points.setdefault(cname, {}).setdefault(rid, []).append(p)
            records += [
                {"config": cname, "repeat": rep, "id": rid, "response": resp[rid],
                 "source": j.input_source, "tier": j.output_tier, "calls": j.calls, "errors": list(j.errors)}
                for rid, j in out.judgements.items()
            ]  # fmt: skip
        summary["configs"][cname] = {"config_hash": config_hash(cfg), "scores": scores}
    if len(points) > 1 and DEFAULT in points:
        summary["compare"] = _compare(points, split)
    spent = sum(e == "budget" for _, e, _ in tap.records)
    if spent:
        summary["invalid"] = f"budget 오류 {spent}회: 결과를 비교에 쓰지 않는다"
    _write(Path(args.out) / args.name, records, summary, render_report(args.name, summary))
    _say(f"wrote {Path(args.out) / args.name}")
    if spent:
        sys.stderr.write(f"error: 호출 상한에 걸려 budget 오류 {spent}회. 결과는 무효다\n")
        return EXIT_BUDGET_HIT
    return 0


def _compare(points: Mapping[str, Mapping[str, list[float]]], split: cv.Split) -> dict:
    mean = {c: {i: sum(v) / len(v) for i, v in rows.items()} for c, rows in points.items()}
    out = {}
    for cname, rows in mean.items():
        if cname == DEFAULT:
            continue
        diff = {i: rows[i] - mean[DEFAULT][i] for i in rows if i in mean[DEFAULT]}
        lo, hi = cluster_bootstrap_ci(diff, split.cluster)
        out[cname] = {"mean": sum(diff.values()) / len(diff), "low": lo, "high": hi}
    return out


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(amain(argv))


if __name__ == "__main__":
    raise SystemExit(main())
