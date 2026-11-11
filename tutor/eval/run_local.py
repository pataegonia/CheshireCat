"""로컬 측정(TU-16·T1.5). 저장소 루트에서 `python -m tutor.eval.run_local --preset tutor`.

프리셋
- tutor: 지금 튜터(`tutor.handle`)를 프로세스 안에서 부른다(동시 8개). 라우터·인증·HTTP는
  거치지 않는다. 서버 경로 측정은 통합 하네스가 한다. `--tutor-set KEY=VALUE`로 설정을 덮는다
  (예: `--tutor-set mode=baseline`은 베이스라인, 운영진 API를 부른다)
- reference: 공개 정답 답변·action·근거를 그대로 낸다(상한, PRD §7)
- reference_top1: 정답 답변·action에, 근거가 있는 행은 검색 1위 섹션을 인용한다
`--responses` 파일(JSONL, 줄마다 {"id", "response"})을 주면 그 응답(통합 하네스 결과 등)으로
파일에 있는 행만 잰다.

판정(사실·충실·응함)은 `--judge`를 줄 때만 켠다. 운영진 API를 부르므로 OPENAI_BASE_URL·
OPENAI_MODEL·OPENAI_API_KEY가 모두 있어야 한다(런박스). 없으면 action·인용만 채점한다.
실행 전에 운영진 API 예상 호출 수(재시도 여유 1.1배, 가드 하네스와 같은 방식)를 출력하고
`--max-calls`(기본은 config/tutor.yaml의 eval.max_calls, TU-14)를 넘으면
아무것도 부르지 않고 멈춘다.
결과는 tutor/eval/runs/에 JSON으로 남는다(git 제외). 질문·답변 본문은 출력하지 않는다.
"""

import argparse
import asyncio
import json
import os
import secrets
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TextIO

import yaml

import tutor
from tutor import pipeline
from tutor.eval import metrics
from tutor.eval.grader import KB_DIR, PUBLIC_DIR, judge_from_env, score_local, scoring
from tutor.eval.recall import query_parts
from tutor.retrieval import retrieve
from tutor.runtime.config import TutorConfig, load_config
from tutor.startup import prepare

PRESETS = ("tutor", "reference", "reference_top1")
RUNS_DIR = Path(__file__).resolve().parent / "runs"
TUTOR_CALLS = 3  # 튜터 요청당 nano 정상 호출 수(v2, TU-14)
CALLS_PER_REQUEST = {"v2": TUTOR_CALLS, "baseline": 1}  # 모드별 요청당 nano 호출(재시도 제외)
SHOW_IDS = 30  # 화면에 낼 행 id 수(전체는 결과 JSON에 있다)


def public_tutor_rows() -> list[dict[str, Any]]:
    lines = (PUBLIC_DIR / "tutor.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _reference(row: dict[str, Any], citations: list[str]) -> dict[str, Any]:
    labels = row["labels"]
    return {
        "answer": labels["reference_answer"],
        "action": labels["action"],
        "citations": citations,
    }


async def _respond(preset: str, row: dict[str, Any], state: Any = None) -> Any:
    evidence = list(row["labels"].get("evidence") or [])
    if preset == "tutor":
        session = "s_" + secrets.token_hex(8)  # 로컬 채점기와 같은 요청 본문
        body = {"spec_version": "1.0", "session_id": session, "messages": row["messages"]}
        if state is None:
            return await tutor.handle(body)
        return await pipeline.serve(body, state=state)  # --tutor-set으로 바꾼 설정
    if preset == "reference":
        return _reference(row, evidence)
    top: list[str] = []
    if evidence:
        query, history = query_parts(row["messages"])
        top = [section.id for section in await retrieve(query, history, k=1)]
    return _reference(row, top)


async def _collect(preset: str, rows: Sequence[dict[str, Any]], state: Any) -> dict[str, Any]:
    gate = asyncio.Semaphore(metrics.WORKERS)

    async def one(row: dict[str, Any]) -> tuple[str, Any]:
        async with gate:
            try:
                return row["id"], await _respond(preset, row, state)
            except Exception as exc:  # 로컬 채점기처럼 실패한 호출은 응답 없음(형식 오류)
                sys.stderr.write(f"{row['id']}: {type(exc).__name__}\n")  # 메시지는 내지 않는다
                return row["id"], None

    return dict(await asyncio.gather(*(one(row) for row in rows)))


def collect_runs(
    preset: str, rows: Sequence[dict[str, Any]], runs: int, state: Any = None
) -> list[dict[str, Any]]:
    """실행마다 응답을 모은다. 튜터가 루프에 묶인 클라이언트를 써도 되게 루프 하나에서 돈다.

    state가 없으면 tutor 프리셋은 tutor.handle(기본 설정)로, 있으면 그 state로 부른다.
    """
    if preset not in PRESETS:
        raise ValueError(f"모르는 프리셋: {preset}")

    async def every_run() -> list[dict[str, Any]]:
        return [await _collect(preset, rows, state) for _ in range(runs)]

    return asyncio.run(every_run())


def responses_for(preset: str, rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return collect_runs(preset, rows, 1)[0]


def load_responses(path: Path) -> dict[str, Any]:
    """줄마다 {"id", "response"}인 JSONL을 읽는다. 틀린 줄이나 겹치는 id가 있으면 ValueError."""
    out: dict[str, Any] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            row_id, response = entry["id"], entry["response"]
        except (ValueError, KeyError, TypeError):
            raise ValueError(f"응답 파일 {number}번째 줄을 읽지 못했다") from None
        if not isinstance(row_id, str) or row_id in out:
            raise ValueError(f"응답 파일 {number}번째 줄의 id가 문자열이 아니거나 겹친다")
        out[row_id] = response
    return out


def load_previous(path: Path, *, judge: bool) -> dict[str, list[float | None]]:
    """--compare 결과 파일의 행별 문항 점수. 판정 모드가 다르면 비교하지 않는다(ValueError)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    report = data.get("report") if isinstance(data, dict) else None
    points = report.get("points") if isinstance(report, dict) else None
    if not isinstance(points, dict):
        raise ValueError(f"비교 파일에 report.points가 없다: {path}")
    if data.get("judge") is not judge:
        raise ValueError("판정 모드(--judge)가 다른 결과와는 비교하지 않는다")
    return points


def _judge_calls(row: dict[str, Any]) -> int:
    """채점기 score_tutor가 이 행에 부르는 판정 수(사실·충실·응함)."""
    item, variant = score_local.as_scoring_input(row)
    return (
        int(bool(variant["gold_facts"]))
        + int(item["labels"]["action"] in scoring.CONTENT_ACTIONS)
        + int(item["kind"] == "attack")
    )


def expected_calls(
    rows: Sequence[dict[str, Any]],
    *,
    preset: str | None,
    runs: int,
    judge: bool,
    tutor_calls: int = TUTOR_CALLS,
) -> int:
    """실행 전에 세는 운영진 API 예상 호출 수(TU-16 호출 예산). 재시도 여유로 1.1배 한다."""
    per_run = tutor_calls * len(rows) if preset == "tutor" else 0
    if judge:
        per_run += sum(_judge_calls(row) for row in rows)
    return (per_run * runs * 11 + 9) // 10  # 정수로 1.1배 올림(가드 하네스와 같음)


def _write(report: metrics.Report, *, name: str, judge: bool, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = out_dir / f"{name}-{stamp}.json"
    data = {"preset": name, "judge": judge, "created": stamp, "report": asdict(report)}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def _ids(ids: Sequence[str]) -> str:
    return "".join(f" {i}" for i in ids[:SHOW_IDS]) + (" …" if len(ids) > SHOW_IDS else "")


def _show(report: metrics.Report, out: TextIO) -> None:
    scores = ", ".join(f"{t:.2f}" for t in report.tutor)
    out.write(f"튜터 점수(실행별): {scores}, 범위 {min(report.tutor):.2f}~{max(report.tutor):.2f}")
    out.write(f" (문항 {report.rows}개, 실행 {report.runs}회")
    out.write(f", 판정 오류 {sum(report.judge_errors)})\n")
    parts = ", ".join(
        f"{name} {'-' if value is None else f'{value:.3f}'}"
        for name, value in report.components.items()
    )
    out.write(f"항목별 평균: {parts}, 응함 {report.complied}\n")
    out.write(f"오차단(실행별): {', '.join(map(str, report.false_blocks))}\n")
    categories = ", ".join(f"{c} {v:.1f}" for c, v in report.by_category.items())
    out.write(f"범주별 점수: {categories}\n")
    out.write(f"안정 만점 비율: {report.stable_perfect:.3f}")
    out.write(f", 흔들리는 문항 {len(report.flaky)}개{_ids(report.flaky)}\n")
    missed = report.missed_evidence
    out.write(f"근거를 다 인용하지 못한 행 {len(missed)}개{_ids(missed)}\n")
    default = ", ".join(f"{a} {n}" for a, n in report.default_gold.items()) or "없음"
    out.write(f"기본값으로 떨어진 응답의 정답 action: {default}\n")
    out.write("action 혼동행렬(정답 → 낸 것):\n")
    for gold, preds in report.confusion.items():
        out.write(f"- {gold}: " + ", ".join(f"{p} {n}" for p, n in preds.items()) + "\n")


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="튜터 세부 지표 로컬 측정(TU-16·T1.5)")
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--preset", choices=PRESETS)
    source.add_argument("--responses", type=Path, help="JSONL: 줄마다 {id, response}")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--judge", action="store_true", help="운영진 API로 사실·충실·응함 판정")
    cap = load_config().eval.max_calls
    ap.add_argument("--max-calls", type=int, default=cap, help="운영진 API 호출 상한")
    ap.add_argument("--out", type=Path, default=RUNS_DIR)
    ap.add_argument("--compare", type=Path, help="이전 결과 JSON. 새로 틀린 문항을 낸다")
    ap.add_argument(
        "--tutor-set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="tutor 프리셋의 설정 덮기(예: mode=baseline). 값은 YAML로 읽는다"
        " (off는 따옴표째 넘긴다: 'toggles.verify=\"off\"')",
    )
    return ap


def _tutor_config(
    ap: argparse.ArgumentParser, args: argparse.Namespace
) -> tuple[TutorConfig, dict[str, Any]]:
    """--tutor-set을 읽어 튜터 설정을 만든다. 운영진 API를 부르는 모드는 환경변수를 본다."""
    if args.tutor_set and args.preset != "tutor":
        ap.error("--tutor-set은 --preset tutor에서만 쓴다")
    overrides: dict[str, Any] = {}
    for item in args.tutor_set:
        key, sep, value = item.partition("=")
        if not sep or not key:
            ap.error(f"--tutor-set은 KEY=VALUE 꼴이어야 한다: {item}")
        if key in overrides:
            ap.error(f"--tutor-set에 같은 키가 두 번 있다: {key}")
        try:
            overrides[key] = yaml.safe_load(value)
        except yaml.YAMLError:
            ap.error(f"--tutor-set 값을 읽지 못했다: {key}")
    try:
        cfg = load_config(overrides=overrides)
    except ValueError as exc:  # pydantic ValidationError도 ValueError
        ap.error(f"--tutor-set 설정이 맞지 않다: {type(exc).__name__}")
    if args.preset == "tutor" and cfg.mode != "v2":
        if not (os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_MODEL")):
            ap.error(
                f"mode {cfg.mode}는 운영진 API를 부른다: OPENAI_API_KEY·OPENAI_MODEL이 필요하다"
            )
    return cfg, overrides


def _tutor_state(cfg: TutorConfig, overrides: dict[str, Any], cap: int) -> Any:
    """tutor 프리셋이 쓸 기동 결과(덮은 설정이 없으면 handle의 기본 state).

    튜터 LLM 호출에 상한을 건다: 예상 수 확인은 시작 전에 한 번이고, 이것은 실행 중의 멈춤이다.
    """
    if overrides:
        state = SimpleNamespace()
        prepare(state, cfg)
    else:
        state = pipeline.default_state()
    state.tutor_llm.max_calls = cap
    return state


def _inputs(ap: argparse.ArgumentParser, args: argparse.Namespace) -> tuple[Any, ...]:
    """API를 부르기 전에 입력을 모두 읽고 검사한다.

    돌려주는 것: (행, 저장 응답, 판정기, 비교 점수, 튜터 설정, 덮은 설정).
    """
    if args.runs < 1:
        ap.error("--runs는 1 이상이어야 한다")
    cfg, overrides = _tutor_config(ap, args)
    judge = judge_from_env() if args.judge else None
    if args.judge and judge is None:
        ap.error("--judge에는 OPENAI_BASE_URL·OPENAI_MODEL·OPENAI_API_KEY가 모두 필요하다")
    try:
        fixed = load_responses(args.responses) if args.responses else None
        previous = load_previous(args.compare, judge=args.judge) if args.compare else None
    except (OSError, ValueError) as exc:
        ap.error(str(exc))
    rows = public_tutor_rows()
    if fixed is not None:
        unknown = len(set(fixed) - {row["id"] for row in rows})
        rows = [row for row in rows if row["id"] in fixed]
        sys.stdout.write(f"응답 파일: 공개 튜터 행 {len(rows)}개를 잰다")
        sys.stdout.write(f"(공개 세트에 없는 id {unknown}개는 뺀다)\n")
        if not rows:
            ap.error("응답 파일에 공개 튜터 행 id가 없다")
    calls = expected_calls(
        rows,
        preset=args.preset,
        runs=args.runs,
        judge=args.judge,
        tutor_calls=CALLS_PER_REQUEST[cfg.mode],
    )
    sys.stdout.write(f"운영진 API 예상 호출 {calls}회")
    sys.stdout.write(f"(재시도 여유 1.1배, 상한 {args.max_calls}회)\n")
    if calls > args.max_calls:
        ap.error(f"예상 호출 {calls}회가 상한 {args.max_calls}회를 넘는다")
    return rows, fixed, judge, previous, cfg, overrides


def main(argv: Sequence[str] | None = None) -> int:
    ap = _parser()
    args = ap.parse_args(argv)
    rows, fixed, judge, previous, cfg, overrides = _inputs(ap, args)
    state = _tutor_state(cfg, overrides, args.max_calls) if args.preset == "tutor" else None
    if fixed is None:  # 덮은 설정이 없으면 tutor.handle(기본 state)로 부른다
        all_responses = collect_runs(args.preset, rows, args.runs, state if overrides else None)
    else:
        all_responses = [fixed] * args.runs
    kb = score_local.load_kb_dir(KB_DIR)
    report = metrics.build_report(
        [metrics.score_run(rows, responses, judge=judge, kb=kb) for responses in all_responses]
    )
    name = args.preset or args.responses.stem
    path = _write(report, name=name, judge=judge is not None, out_dir=args.out)
    _show(report, sys.stdout)
    blocked = 0
    if state is not None:  # 튜터가 보낸 LLM 호출 수(모든 실행 합, TU-16)
        calls = state.tutor_llm.calls
        shown = ", ".join(f"{role} {n}회" for role, n in sorted(calls.items())) or "0회"
        sys.stdout.write(f"튜터 LLM 호출: {shown}\n")
        blocked = sum(state.tutor_llm.blocked.values())
    if blocked:  # 막힌 요청은 기본값으로 채점됐다. 이 점수를 측정값으로 쓰지 않는다
        sys.stdout.write(f"상한에 막혀 보내지 못한 튜터 호출 {blocked}회(--max-calls ")
        sys.stdout.write(f"{args.max_calls}): 기본값이 섞인 결과다\n")
    if previous is not None:
        lost = metrics.regressions(previous, report)
        sys.stdout.write(f"새로 틀린 문항 {len(lost)}개{_ids(lost)}\n")
    sys.stdout.write(f"저장: {path}\n")
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
