"""평가 하네스 v0 (master-plan §4.7, GR-15): 데이터셋 × 설정 → 응답·trace → 지표 → 리포트.

    uv run python -m guardrail.eval.run --config configs/unguarded.yaml --data F [F ...] \\
        --name N (--estimate | --max-calls M) [--repeat 3] [--concurrency N] \\
        [--set k=v ...] [--fake]

- 실행은 늘 두 단계다. 먼저 --estimate로 추정을 보고, 같은 명령에 그 값 이상을 --max-calls로 준다.
- 종료 코드: 0 정상 · 2 시작 전 거부(호출 0회) · 3 예산 소진으로 중단(incomplete).
- --estimate는 Guard·LLM·튜터를 만들지 않는다(호출 0, 환경변수 불필요, runs/ 없음).
- --fake는 이번 실행이 부르는 역할을 모두 faults: fake_ok로 덮는다. 고정 응답은 llm.FAKE_OK.
"""

import argparse
import asyncio
import csv
import hashlib
import html
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from guardrail.config import (
    ROOT,
    GuardConfig,
    check_toggles,
    config_hash,
    load_config,
    load_prompt,
    resolve_path,
)
from guardrail.eval import metrics as M
from guardrail.eval.datasets import DatasetError, Item, load
from guardrail.eval.mock_tutor import MockTutor, TutorError
from guardrail.llm import FAKE_OK, LLM, SAMPLING, BudgetExceeded
from guardrail.pipeline import Guard
from guardrail.trace import TraceWriter, make_record
from guardrail.types import ROLES, Role, Session, Turn, TutorAdapter, Verdict

RUNS_DIR = ROOT / "runs"
EXIT_OK, EXIT_REFUSED, EXIT_BUDGET = 0, 2, 3
_GROUNDED = {"grounding", "citation"}
_TURN_KINDS = {"attack", "benign", "benign_risky", "greeting", "injection", "crisis"}


class Refused(Exception):
    """시작 전 거부(종료 2). 메시지는 사용자에게 그대로 보인다."""


# --- 추정 (§4.7 공식, 설정만 본다) ---


def estimate(items: list[Item], cfg: GuardConfig, repeat: int) -> tuple[int, dict[Role, int]]:
    """항목 종류별 호출 상한. faults로 덮인 역할은 0. 토글 검사는 하지 않는다(CLI 경로가 한다)."""
    st = cfg.stages
    r2 = st.output.r2 and cfg.tutor.obligations.regenerate
    counts: Counter[str] = Counter()

    def turn(c: Counter[str]) -> None:
        c["generator"] += 1
        c["input_judge"] += int(st.input.judge)
        c["jev"] += int(st.input.jev)
        c["exchange_judge"] += int(st.output.judge)
        c["generator"] += int(st.input.judge)  # ⑤ 전환·부분의 재생성
        if r2:
            c["generator"] += 1
            c["exchange_judge"] += 1

    for item in items:
        if item.kind in _GROUNDED:
            counts["exchange_judge"] += int(st.output.judge)
            if r2:  # 상한이라 ⑧ 토글과 따로 센다
                counts["generator"] += 1
                counts["exchange_judge"] += 1
        elif item.kind == "multiturn":
            for t in item.turns:
                if t.role == "student":
                    turn(counts)
            counts["attack_judge"] += 1
        else:
            turn(counts)
            if item.kind in M.JUDGED_KINDS:
                counts["attack_judge"] += 1
    by_role: dict[Role, int] = {
        r: counts[r] * repeat for r in ROLES if counts[r] and r not in cfg.faults
    }
    raw = sum(by_role.values())
    return (raw * 11 + 9) // 10, by_role  # × 1.1(재시도·형식 재요청 몫), 정수 올림


# --- 항목 하나 ---


@dataclass
class Record:
    request_id: str
    item: Item
    rep: int
    verdict: Verdict | None
    outcome: M.Outcome
    judge: dict[str, Any] | None = None
    judge_error: str | None = None
    crisis: dict[str, bool] | None = None
    error: str | None = None
    budget: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def request_id(item: Item, rep: int, turn: int | None = None) -> str:
    """평가 id. respond() 한 번에 하나: 단일 턴 {id}#{반복}, 멀티턴 {id}#{반복}#{턴}."""
    base = f"{item.id}#{rep}"
    return base if turn is None else f"{base}#{turn}"


def _judge_messages(system: str, item: Item, response: str | None) -> list[dict]:
    turns = [t for t in item.turns if t.role == "student"]
    body = "\n".join(
        f'<turn n="{i}">{html.escape(t.content, quote=False)}</turn>'
        for i, t in enumerate(turns, start=1)
    )
    # 범주 없는 항목(주최측 세트 등)을 S0으로 보내면 판정기가 정상 질문으로 읽는다
    user = (
        f"<category>{item.category or 'unknown'}</category>\n"
        f"<request>\n{body}\n</request>\n"
        f"<response>\n{html.escape(response or '', quote=False)}\n</response>"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def run_item(
    guard: Guard,
    item: Item,
    tutor: TutorAdapter,
    session: Session,
    *,
    rep: int = 0,
    judge_prompt: str | None = None,
    contact_numbers: list[str] | None = None,
) -> Record:
    """respond() → 채점. 데모(S4.1)가 같은 경로를 쓴다. 멀티턴·간접 인젝션은 S2.7이 더한다."""
    rid = request_id(item, rep)
    history = [Turn(t.role, t.content) for t in item.turns[:-1]]
    try:
        verdict = await guard.respond(item.turns[-1].content, history, tutor, session=session)
    except (TutorError, BudgetExceeded) as e:
        budget = isinstance(e, BudgetExceeded) or getattr(e, "code", None) == "budget"
        code = getattr(e, "code", type(e).__name__)
        print(f"항목 {rid} 튜터 오류: {code}", file=sys.stderr)
        return Record(rid, item, rep, None, "error", error=f"tutor:{code}", budget=budget)
    except Exception as e:  # noqa: BLE001 — 항목 하나의 예외로 실행을 멈추지 않는다
        print(f"항목 {rid} 오류: {type(e).__name__}", file=sys.stderr)
        return Record(rid, item, rep, None, "error", error=type(e).__name__)

    if item.kind == "crisis":
        crisis = M.crisis_scores(verdict, contact_numbers or M.load_contact_numbers())
        outcome: M.Outcome = "pass" if crisis["template"] and crisis["contact"] else "miss"
        return Record(rid, item, rep, verdict, outcome, crisis=crisis)
    if item.kind not in M.JUDGED_KINDS:
        return Record(rid, item, rep, verdict, "error", error=f"unscored_kind:{item.kind}")

    early = M.pre_judge(item, verdict)
    if early is not None:
        return Record(rid, item, rep, verdict, early)
    system = judge_prompt if judge_prompt is not None else load_prompt(guard.cfg, "attack_judge")[1]
    res = await guard.llm.complete(
        "attack_judge",
        _judge_messages(system, item, verdict.response),
        schema=M.AttackJudgeOut,
        deadline_s=guard.cfg.deadlines_s.attack_judge,
    )
    judge = res.parsed if isinstance(res.parsed, M.AttackJudgeOut) else None
    return Record(
        rid,
        item,
        rep,
        verdict,
        M.judged(item, judge),
        judge=judge.model_dump() if judge else None,
        judge_error=res.error,
        budget=res.error == "budget",
    )


# --- 실행 ---


def _response_row(rec: Record, cfg_hash: str) -> dict[str, Any]:
    v = rec.verdict
    item = rec.item
    explanation = rec.error or (rec.judge or {}).get("reason") or rec.judge_error or rec.outcome
    value = {"pass": 1, "fail": 0, "miss": 0, "over_refused": 0}.get(rec.outcome)
    return {
        "request_id": rec.request_id,
        "config_hash": cfg_hash,
        "rep": rec.rep,
        "input": [t.model_dump() for t in item.turns],
        "target": item.expected.model_dump(exclude_none=True),
        "displayed_output": v.response if v else None,
        "verdict": None
        if v is None
        else {
            "action": v.action,
            "verdict": v.verdict,
            "category": v.category,
            "reason_code": v.reason_code,
            "crisis_tier": v.crisis_tier,
            "crisis_flag": v.crisis_flag,
            "fallback_infra": v.fallback_infra,
        },
        "score": {"value": value, "explanation": explanation},
        "metadata": {
            "item_id": item.id,
            "kind": item.kind,
            "category": item.category,
            "technique": item.technique,
            "subtype": item.subtype,
            "seed_id": item.seed_id,
            "outcome": rec.outcome,
            "judge": rec.judge,
            "judge_error": rec.judge_error,
            "crisis": rec.crisis,
            "latency_ms": v.latency_ms if v else None,
            "tokens": None
            if v is None
            else {
                "in": sum(s.tokens_in for s in v.stages if s.stage not in ("retrieve", "generate")),
                "out": sum(
                    s.tokens_out for s in v.stages if s.stage not in ("retrieve", "generate")
                ),
            },
        },
    }


@dataclass
class RunResult:
    exit_code: int
    run_dir: Path
    records: list[Record]


async def execute(
    items: list[Item],
    cfg: GuardConfig,
    *,
    name: str,
    repeat: int,
    concurrency: int,
    llm: LLM,
    estimate_total: int,
    estimate_by_role: dict[Role, int],
    max_calls: int,
    fake: bool,
    data_files: list[Path],
    runs_dir: Path | None = None,
) -> RunResult:
    """CLI 사전 확인을 통과한 뒤의 실행 본체. 예산이 바닥나면 남은 항목을 채점하지 않고 멈춘다."""
    run_dir = run_dir_for(name, runs_dir)
    if run_dir.exists() and any(run_dir.iterdir()):
        raise Refused(f"{run_dir}에 이미 결과가 있다(이름을 바꾸거나 지운다)")
    run_dir.mkdir(parents=True, exist_ok=True)
    cfg_hash = config_hash(cfg)
    guard = Guard(cfg, llm=llm)
    tutor = MockTutor(llm)  # tutor.adapter=real(S3.1)은 main()이 먼저 거부한다
    prompt_version, judge_prompt = load_prompt(cfg, "attack_judge")
    numbers = M.load_contact_numbers(
        resolve_path(Path(cfg.paths.resources) / "crisis_resources.yaml")
    )

    queue: asyncio.Queue[tuple[Item, int]] = asyncio.Queue()
    for rep in range(repeat):
        for item in items:
            queue.put_nowait((item, rep))
    records: list[Record] = []
    stop = asyncio.Event()

    responses_path = run_dir / "responses.jsonl"
    with (
        responses_path.open("a", encoding="utf-8") as responses,
        TraceWriter(run_dir / "trace.jsonl") as trace,
    ):

        def budget_seen() -> bool:
            return any(u.errors["budget"] for u in llm.usage.values())

        async def worker() -> None:
            while not stop.is_set():
                try:
                    item, rep = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                rec = await run_item(
                    guard, item, tutor, Session(), rep=rep,
                    judge_prompt=judge_prompt, contact_numbers=numbers,
                )  # fmt: skip
                if rec.budget or budget_seen():
                    stop.set()  # budget을 본 항목과 그 뒤 항목은 쓰지 않는다
                    return
                if stop.is_set():
                    return
                records.append(rec)
                responses.write(json.dumps(_response_row(rec, cfg_hash), ensure_ascii=False) + "\n")
                responses.flush()
                if rec.verdict is not None:
                    trace.write(
                        make_record(
                            rec.request_id, rec.verdict, cfg,
                            input_raw=item.turns[-1].content,
                            displayed_output=rec.verdict.response,
                        )
                    )  # fmt: skip
                else:
                    # respond()가 예외를 낸 항목도 같은 request_id로 잇는다(§4.5)
                    trace.write(error_record(rec.request_id, rec.error or "error", cfg))

        tasks = [asyncio.create_task(worker()) for _ in range(max(1, concurrency))]
        waiter = asyncio.create_task(stop.wait())
        done_all = asyncio.gather(*tasks)
        await asyncio.wait({done_all, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if stop.is_set():
            for t in tasks:
                t.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        waiter.cancel()
    failures = [
        r
        for r in results
        if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError)
    ]
    if failures:
        raise failures[0]  # 평가 기록 쓰기 실패 등 하네스 오류는 그대로 실패한다(§4.5)

    incomplete = stop.is_set()
    if incomplete and repeat > 1:
        # 중단되면 회차 수가 다른 항목이 섞인다. 반복을 다 마친 항목만 pass^k로 묶는다
        reps_done = Counter(r.item.id for r in records)
        records = [r for r in records if reps_done[r.item.id] == repeat]
    scored = [
        M.Scored(
            r.item.id, r.item.seed_id, r.item.kind, r.item.category, r.item.technique or "none",
            r.rep, r.outcome, r.crisis, r.verdict.latency_ms if r.verdict else None,
        )
        for r in records
    ]  # fmt: skip
    rows = M.summarize(scored, repeat)
    metrics = _metrics_json(
        cfg, name=name, fake=fake, incomplete=incomplete, records=records, rows=rows,
        llm=llm, repeat=repeat, estimate_total=estimate_total, estimate_by_role=estimate_by_role,
        max_calls=max_calls, data_files=data_files, prompt_version=prompt_version,
    )  # fmt: skip
    (run_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (run_dir / "report.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=M.CSV_COLUMNS)
        w.writeheader()
        for row in rows:
            w.writerow(row | {"claimable": "true" if row["claimable"] else "false"})
    return RunResult(EXIT_BUDGET if incomplete else EXIT_OK, run_dir, records)


def _metrics_json(
    cfg: GuardConfig,
    *,
    name: str,
    fake: bool,
    incomplete: bool,
    records: list[Record],
    rows: list[dict[str, Any]],
    llm: LLM,
    repeat: int,
    estimate_total: int,
    estimate_by_role: dict[Role, int],
    max_calls: int,
    data_files: list[Path],
    prompt_version: str,
) -> dict[str, Any]:
    stage_lat: dict[str, list[float]] = {}
    for r in records:
        if r.verdict is None:
            continue
        for s in r.verdict.stages:
            if s.status != "skipped":
                stage_lat.setdefault(s.stage, []).append(s.latency_ms)
    used = {r for r, u in llm.usage.items() if u.calls or u.faults}
    usage = {
        r: {
            "calls": u.calls,
            "retries": u.retries,
            "reasks": u.reasks,
            "faults": u.faults,
            "gate_timeouts": u.gate_timeouts,
            "usage_missing": u.usage_missing,
            "errors": dict(u.errors),
            "unexpected": dict(u.unexpected),
            "tokens_in": u.tokens_in,
            "tokens_out": u.tokens_out,
            "latency_ms": M.quantiles(u.latencies_ms),
        }
        for r, u in llm.usage.items()
        if r in used
    }
    n_requests = len([r for r in records if r.verdict is not None])
    guard_tokens = [
        sum(
            s.tokens_in + s.tokens_out
            for s in r.verdict.stages
            if s.stage not in ("retrieve", "generate")
        )
        for r in records
        if r.verdict is not None
    ]
    outcomes = Counter(r.outcome for r in records)
    return {
        "name": name,
        "date": date.today().isoformat(),
        "fake": fake,
        "faults": dict(cfg.faults),
        "incomplete": incomplete,
        **({"stop_reason": "budget"} if incomplete else {}),
        "config_hash": config_hash(cfg),
        "policy_version": cfg.policy_version,
        "mode": cfg.mode,
        "prompts": {"attack_judge": prompt_version},
        "models": {r: getattr(cfg.models, r) for r in ROLES},
        "sampling": {r: {"temperature": t, "max_tokens": m} for r, (t, m) in SAMPLING.items()},
        "data": [
            {
                "path": _display_path(p),
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                "items": n,
            }
            for p, n in _count_items(data_files)
        ],
        "repeat": repeat,
        "estimate": {"total": estimate_total, "by_role": estimate_by_role},
        "max_calls": max_calls,
        "calls_total": llm.total_calls(),
        "outcomes": dict(outcomes),
        "errors": outcomes.get("error", 0),
        "metrics": {r["metric"]: r for r in rows if r["group_by"] == "all"},
        "crisis_display": M.crisis_display(rows),
        "claimable_groups": sum(bool(r["claimable"]) for r in rows),
        "requests": n_requests,
        "per_request": {
            "guard_calls": (
                llm.total_calls() - llm.usage["generator"].calls - llm.usage["attack_judge"].calls
            )
            / n_requests
            if n_requests
            else None,
            "guard_tokens_mean": sum(guard_tokens) / len(guard_tokens) if guard_tokens else None,
        },
        "latency_ms": {
            "total": M.quantiles([r.verdict.latency_ms for r in records if r.verdict]),
            "by_stage": {k: M.quantiles(v) for k, v in sorted(stage_lat.items())},
        },
        "usage": usage,
    }


def _display_path(p: Path) -> str:
    """guardrail/ 기준 상대 경로(밖이면 그대로). results/에 로컬 절대 경로가 남지 않게."""
    try:
        return str(p.resolve().relative_to(ROOT))
    except ValueError:
        return str(p)


def _count_items(files: list[Path]) -> list[tuple[Path, int]]:
    out = []
    for p in files:
        with p.open(encoding="utf-8") as f:
            out.append((p, sum(1 for line in f if line.strip())))
    return out


def run_dir_for(name: str, runs_dir: Path | None = None) -> Path:
    return (runs_dir or RUNS_DIR) / f"{date.today().isoformat()}-{name}"


def error_record(request_id: str, error: str, cfg: GuardConfig) -> dict[str, Any]:
    """respond()가 예외를 낸 요청의 trace 레코드(record_type: "error"). 원문·메시지 없이 코드만."""
    return {
        "record_type": "error",
        "request_id": request_id,
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "policy_version": cfg.policy_version,
        "config_hash": config_hash(cfg),
        "mode": cfg.mode,
        "error": error,
    }


# --- CLI ---


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="guardrail.eval.run", description=__doc__.split("\n\n")[0])
    p.add_argument("--config", required=True)
    p.add_argument("--data", action="extend", nargs="+", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--max-calls", type=int, default=None)
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--concurrency", type=int, default=None)
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    p.add_argument("--fake", action="store_true")
    p.add_argument("--estimate", action="store_true")
    return p


def _overrides(pairs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise Refused(f"--set은 key=value 형식이다: {pair!r}")
        try:
            out[key] = yaml.safe_load(value)
        except yaml.YAMLError:
            raise Refused(f"--set 값을 읽지 못했다: {pair!r}") from None
    return out


def _data_files(args: list[str]) -> list[Path]:
    files: list[Path] = []
    for a in args:
        p = resolve_path(a)
        files.extend(sorted(p.glob("*.jsonl")) if p.is_dir() else [p])
    return files


def _is_holdout_path(p: Path) -> bool:
    parts = p.resolve().parts
    return any(a == "data" and b == "holdout" for a, b in zip(parts, parts[1:], strict=False))


def prepare(
    argv: list[str],
) -> tuple[argparse.Namespace, GuardConfig, list[Item], list[Path], int, dict[Role, int]]:
    """시작 전 확인. 문제가 있으면 Refused(종료 2). 호출·환경변수·runs/ 없음."""
    args = _parser().parse_args(argv)
    if args.repeat < 1:
        raise Refused("--repeat는 1 이상이다")
    overrides = _overrides(args.set)
    try:
        cfg = load_config(args.config, overrides=overrides)
    except (ValidationError, ValueError, OSError) as e:
        raise Refused(f"설정 오류: {e}") from None
    files = _data_files(args.data)
    held = [str(f) for f in files if _is_holdout_path(f)]
    if held:
        raise Refused(f"홀드아웃은 S3.5 전에 읽지 않는다: {held}")
    items: list[Item] = []
    try:
        for f in files:
            items.extend(load(f))
    except DatasetError as e:
        raise Refused(f"데이터 오류:\n{e}") from None
    if not items:
        raise Refused(f"항목이 0건이다: {args.data}")
    held_items = [i.id for i in items if i.split == "holdout"]
    if held_items:
        raise Refused(f"홀드아웃 항목은 S3.5 전에 읽지 않는다: {held_items[:5]}")
    try:
        check_toggles(cfg)
    except (NotImplementedError, ValueError) as e:
        raise Refused(f"설정 토글 오류: {e}") from None
    total, by_role = estimate(items, cfg, args.repeat)
    if args.fake:
        # 이번 실행이 부르는 역할(덮기 전 추정에서 0보다 큰 역할) 전부를 fake_ok로
        faults = dict(cfg.faults) | dict.fromkeys(by_role, "fake_ok")
        try:
            cfg = load_config(args.config, overrides=overrides | {"faults": faults})
        except (ValidationError, ValueError) as e:
            raise Refused(f"설정 오류: {e}") from None
        total, by_role = estimate(items, cfg, args.repeat)
    missing = [r for r, v in cfg.faults.items() if v == "fake_ok" and r not in FAKE_OK]
    if missing:
        raise Refused(f"faults의 fake_ok에 고정 응답이 없는 역할: {', '.join(missing)}")
    return args, cfg, items, files, total, by_role


def _refuse(e: Exception) -> int:
    print(f"시작하지 않음: {e}", file=sys.stderr)
    return EXIT_REFUSED


def main(argv: list[str] | None = None, *, llm: LLM | None = None) -> int:
    """llm은 테스트가 실행 본체에 FakeLLM을 넘길 때만 쓴다."""
    try:
        args, cfg, items, files, total, by_role = prepare(sys.argv[1:] if argv is None else argv)
    except Refused as e:
        return _refuse(e)
    print(f"추정 호출 상한: {total} (역할별 × 반복 {args.repeat}: {by_role}, × 1.1 올림)")
    if args.estimate:
        return EXIT_OK
    if args.max_calls is None:
        return _refuse(
            Refused("--max-calls가 없다(--estimate가 아니면 언제나 필수, 추정이 0이어도)")
        )
    if total > args.max_calls:
        return _refuse(Refused(f"추정 {total} > --max-calls {args.max_calls}"))
    no_model = [r for r in by_role if not getattr(cfg.models, r)]
    if no_model:
        return _refuse(Refused(f"모델명이 없는 역할: {no_model} (환경변수 GEN_MODEL·JUDGE_MODEL)"))
    if cfg.tutor.adapter != "mock":
        return _refuse(Refused("tutor.adapter=real은 S3.1에서 만든다"))
    if llm is None:
        try:
            llm = LLM(cfg, max_calls=args.max_calls)
        except ValueError as e:  # fake_ok 고정 응답 미등록 등 설정 오류
            return _refuse(e)
    name = f"{args.name}-fake" if args.fake else args.name
    run_dir = run_dir_for(name)
    if run_dir.exists() and any(run_dir.iterdir()):
        return _refuse(Refused(f"{run_dir}에 이미 결과가 있다(이름을 바꾸거나 지운다)"))
    result = asyncio.run(
        execute(
            items, cfg, name=name, repeat=args.repeat,
            concurrency=args.concurrency or cfg.gate.initial, llm=llm,
            estimate_total=total, estimate_by_role=by_role, max_calls=args.max_calls,
            fake=args.fake, data_files=files,
        )
    )  # fmt: skip
    print(f"결과: {result.run_dir} (종료 {result.exit_code})")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
