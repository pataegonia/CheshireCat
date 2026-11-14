"""로컬 측정(TU-16·T1.5). 저장소 루트에서 `python -m tutor.eval.run_local --preset tutor`.

프리셋
- tutor: 지금 튜터(`tutor.handle`)를 프로세스 안에서 부른다(동시 8개). 라우터·인증·HTTP는
  거치지 않는다. 서버 경로 측정은 통합 하네스가 한다. `--tutor-set KEY=VALUE`로 설정을 덮는다
  (예: `--tutor-set mode=baseline`은 베이스라인). v2(주 호출)도 베이스라인도 운영진 API를
  부르므로 OPENAI_BASE_URL·OPENAI_MODEL·OPENAI_API_KEY가 모두 있어야 한다
- reference: 공개 정답 답변·action·근거를 그대로 낸다(상한, PRD §7)
- reference_top1: 정답 답변·action에, 근거가 있는 행은 검색 1위 섹션을 인용한다
`--responses` 파일(JSONL, 줄마다 {"id", "response"})을 주면 그 응답(통합 하네스 결과 등)으로
파일에 있는 행만 잰다.

판정(사실·충실·응함)은 `--judge`를 줄 때만 켠다. 운영진 API를 부르므로 OPENAI_BASE_URL·
OPENAI_MODEL·OPENAI_API_KEY가 모두 있어야 한다(런박스). 없으면 action·인용만 채점한다.
실행 전에 운영진 API 예상 호출 수(재시도 여유 1.1배, 가드 하네스와 같은 방식)를 출력하고
`--max-calls`(기본은 config/tutor.yaml의 eval.max_calls, TU-14)를 넘으면
아무것도 부르지 않고 멈춘다. 실행 중에는 판정 몫(예상 판정 호출)을 뺀 나머지를 튜터 LLM 호출
상한으로 건다.
튜터 LLM 호출의 429(판정은 최종 실패)가 연달아 RATE_LIMIT_STREAK회 이어지면 뒤의 호출(임베딩
포함)을 보내지 않고, 결과를 무효(invalid)로 남기고 종료 코드 EXIT_RATE_LIMITED로 끝난다(가드
하네스와 같은 규칙).
결과는 tutor/eval/runs/에 JSON으로 남는다(git 제외). 질문·답변 본문은 출력하지 않는다.
tutor 프리셋은 튜터 LLM 수(보낸 시도·최종 실패·실패한 시도 갈래·성공한 시도의 토큰 구간)도
결과 JSON의 tutor_llm에 남긴다.
"""

import argparse
import asyncio
import json
import math
import secrets
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TextIO

import yaml

import tutor
from tutor import pipeline
from tutor.eval import metrics
from tutor.eval.grader import KB_DIR, PUBLIC_DIR, judge_from_env, score_local, scoring
from tutor.eval.recall import (
    CountedEmbed,
    english_queries,
    expected_embedding_calls,
    missing_embedding_env,
    query_parts,
)
from tutor.retrieval import retrieve
from tutor.retrieval.embedding import openai_embed
from tutor.runtime.config import TutorConfig, load_config
from tutor.runtime.llm import TOKEN_BUCKET, missing_api_env
from tutor.startup import build_embedding, prepare

PRESETS = ("tutor", "reference", "reference_top1")
RUNS_DIR = Path(__file__).resolve().parent / "runs"
# 튜터 요청당 nano 정상 호출 수(v2 최종 구성 = 주 호출 + 가드 classify + judge_output, TU-14).
# 지금 v2는 주 호출만 해서(#97, classify·judge_output은 #88) 예상이 실제보다 크다(상한 쪽이라 안전).
TUTOR_CALLS = 3
CALLS_PER_REQUEST = {"v2": TUTOR_CALLS, "baseline": 1}  # 모드별 요청당 nano 호출(재시도 제외)
SHOW_IDS = 30  # 화면에 낼 행 id 수(전체는 결과 JSON에 있다)
# 운영진 API 호출이 이만큼 연달아 429로 끝나면(각각 재시도 포함) 더 보내지 않는다. 막힌 키에 계속
# 보내면 어뷰징으로 보일 수 있다(SPEC §7.1). 값과 종료 코드는 가드 하네스(#151)와 같다
RATE_LIMIT_STREAK = 20
EXIT_RATE_LIMITED = 6


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


async def _collect(
    preset: str, rows: Sequence[dict[str, Any]], state: Any
) -> tuple[dict[str, Any], list[float]]:
    """한 실행의 (행 id → 응답, 요청마다 걸린 시간(초)). 실패한 요청도 시간을 잰다."""
    gate = asyncio.Semaphore(metrics.WORKERS)

    async def one(row: dict[str, Any]) -> tuple[str, Any, float]:
        async with gate:
            started = time.perf_counter()
            try:
                response = await _respond(preset, row, state)
            except Exception as exc:  # 로컬 채점기처럼 실패한 호출은 응답 없음(형식 오류)
                sys.stderr.write(f"{row['id']}: {type(exc).__name__}\n")  # 메시지는 내지 않는다
                response = None
            return row["id"], response, time.perf_counter() - started

    done = await asyncio.gather(*(one(row) for row in rows))
    return {row_id: response for row_id, response, _took in done}, [t for _i, _r, t in done]


def collect_timed(
    preset: str, rows: Sequence[dict[str, Any]], runs: int, state: Any = None
) -> tuple[list[dict[str, Any]], list[float]]:
    """실행마다 응답을, 그리고 요청마다 걸린 시간(초, 모든 실행)을 모은다(TU-16 지연).

    튜터가 루프에 묶인 클라이언트를 써도 되게 루프 하나에서 돈다. 동시 요청은 metrics.WORKERS개다.
    state가 없으면 tutor 프리셋은 tutor.handle(기본 설정)로, 있으면 그 state로 부른다.
    """
    if preset not in PRESETS:
        raise ValueError(f"모르는 프리셋: {preset}")

    async def every_run() -> list[tuple[dict[str, Any], list[float]]]:
        return [await _collect(preset, rows, state) for _ in range(runs)]

    results = asyncio.run(every_run())
    return [responses for responses, _t in results], [t for _r, ts in results for t in ts]


def collect_runs(
    preset: str, rows: Sequence[dict[str, Any]], runs: int, state: Any = None
) -> list[dict[str, Any]]:
    """실행마다 응답을 모은다(collect_timed에서 시간을 뺀 것)."""
    return collect_timed(preset, rows, runs, state)[0]


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
    if data.get("tainted"):
        raise ValueError("비교 파일은 기본값이나 BM25가 섞인 결과다(tainted): 기준으로 쓰지 않는다")
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


class RateLimitStop:
    """운영진 API 호출이 RATE_LIMIT_STREAK회 연달아 429로 끝나면 뒤의 호출을 보내지 않는다.

    가드 하네스(#151)와 같은 규칙이다: 호출 하나의 최종 오류(재시도 포함)가 rate_limit이면 연속
    수 +1, budget(상한에 막혀 보내지 않음)이면 그대로, 그 밖(성공 포함)이면 0이다. 튜터 LLM과
    판정은 같은 팀 키라 한 객체가 세되, 단계가 바뀌면 연속 수를 새로 센다(next_stage). 판정은
    스레드에서 돌아 잠금 안에서 센다.
    """

    def __init__(self) -> None:
        self.streak = 0
        self.tripped = False
        self.by: str | None = None  # 멈추게 한 호출: "llm" 또는 "judge"
        self._lock = threading.Lock()

    def record(self, error: str | None, by: str = "llm") -> bool:
        """호출 하나의 최종 오류 코드를 센다. 이번 호출로 멈췄으면 True."""
        with self._lock:
            if error == "rate_limit":
                self.streak += 1
            elif error != "budget":
                self.streak = 0
            if self.streak < RATE_LIMIT_STREAK or self.tripped:
                return False
            self.tripped, self.by = True, by
            return True

    def next_stage(self) -> None:
        """튜터 단계가 끝나면 연속 수를 새로 센다(튜터의 429가 판정 실패와 이어지지 않게)."""
        with self._lock:
            self.streak = 0

    def reason(self) -> str | None:
        """멈춘 사유(결과 JSON의 invalid). 판정기는 429를 가르지 못해 판정 쪽은 단정하지 않는다."""
        if not self.tripped:
            return None
        if self.by == "judge":
            return f"판정 최종 실패(429일 수 있음)가 {RATE_LIMIT_STREAK}회 이어져 멈췄다"
        return f"속도 제한(429)이 {RATE_LIMIT_STREAK}회 이어져 멈췄다"

    def watch_llm(self, llm: Any) -> None:
        """튜터 LLM의 complete를 감싼다(서버 코드는 그대로다).

        멈추면 max_calls를 0으로 내린다. 뒤의 호출과 진행 중인 재시도는 보내지 않고 budget이 된다.
        """
        inner = llm.complete

        async def complete(*args: Any, **kwargs: Any) -> Any:
            result = await inner(*args, **kwargs)
            if self.record(result.error):
                llm.max_calls = 0
            return result

        llm.complete = complete

    def watch_judge(self, judge: metrics.Judge) -> metrics.Judge:
        """판정기를 감싼다. 멈춘 뒤에는 부르지 않고 판정 오류로 둔다.

        운영진 판정기(judge.py)는 예외를 삼키고 JudgeError만 내서 429를 다른 실패와 가르지 못한다.
        그래서 판정의 최종 실패(판정기 재시도 포함)를 429로 센다. 막힌 키에서는 판정이 모두
        실패하고, 그렇지 않아도 연달아 실패한 판정 결과는 쓰지 않는다.
        """

        def watched(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
            if self.tripped:
                raise scoring.JudgeError("속도 제한으로 멈췄다")
            try:
                verdict = judge(kind, payload)
            except Exception:
                self.record("rate_limit", "judge")
                raise
            self.record(None, "judge")
            return verdict

        return watched

    def watch_embed(self, search: Any) -> Any:
        """질의 임베딩을 감싼 EmbeddingSearch를 돌려준다. 멈춘 뒤에는 보내지 않고 예외를 낸다.

        세는 함수(CountedEmbed) 바깥에서 막으므로 막은 질의는 보낸 호출로 세지 않는다. 검색은
        예외가 난 질의를 BM25로 한다. 임베딩은 다시 보내지 않고 호출 수가 시작 전에 정해져 상한
        검사를 받으므로 연속 수에는 넣지 않는다. 같은 팀 키라 멈춘 뒤 남은 질의만 막는다.
        """
        inner = search.embed

        async def embed(texts: Sequence[str]) -> list[list[float]]:
            if self.tripped:
                raise RuntimeError("속도 제한으로 멈췄다")
            return await inner(texts)

        return replace(search, embed=embed)


def _write(
    report: metrics.Report,
    *,
    name: str,
    judge: bool,
    out_dir: Path,
    tainted: bool,
    embedding: "EmbeddingUse | None",
    invalid: str | None,
    llm: Any = None,
) -> Path:
    """결과 JSON을 남긴다. tainted면 기본값이나 BM25가 섞인 결과라 --compare 기준이 되지 못한다.

    invalid는 실행을 멈춘 사유다(연속 429·판정 최종 실패). 있으면 tainted도 참이다.
    llm(튜터 LLM)이 있으면 그 수를 tutor_llm에 남긴다(본문 없음). load_previous는 이 필드를
    읽지 않아, 필드가 없는 옛 결과도 --compare에 쓴다.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = out_dir / f"{name}-{stamp}.json"
    data: dict[str, Any] = {"preset": name, "judge": judge, "created": stamp, "tainted": tainted}
    if invalid is not None:
        data["invalid"] = invalid
    if embedding is not None:
        data["embedding"] = {**asdict(embedding), "ok": embedding.ok}
    if llm is not None:
        data["tutor_llm"] = {
            "calls": dict(sorted(llm.calls.items())),  # 보낸 시도 수(아래 시도 단위 수의 분모)
            "failures": _by_role(llm.failures),  # 호출 단위 최종 오류
            "attempt_errors": _by_role(llm.attempt_errors),  # 시도 단위 갈래(재시도 포함)
            "completion_buckets": _by_role(llm.completion_buckets),  # 성공한 시도의 토큰 구간
        }
    data["report"] = asdict(report)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def _by_role(counts: Mapping[tuple[str, Any], int]) -> dict[str, dict[Any, int]]:
    """(역할, 갈래)별 수를 역할 → 갈래 → 수로 묶는다(갈래 순서대로)."""
    out: dict[str, dict[Any, int]] = {}
    for (role, key), n in sorted(counts.items()):
        out.setdefault(role, {})[key] = n
    return out


def _ids(ids: Sequence[str]) -> str:
    return "".join(f" {i}" for i in ids[:SHOW_IDS]) + (" …" if len(ids) > SHOW_IDS else "")


def _pct(values: Sequence[float], q: float) -> float:
    """가까운 순위 백분위(값이 없으면 0)."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]


def _latency_line(latency: Sequence[float]) -> str:
    """튜터 요청 지연 한 줄(TU-16·TU-17 지표). 프로세스 안 호출이라 HTTP 시간은 빠진다."""
    p50, p95 = _pct(latency, 0.5), _pct(latency, 0.95)
    top = max(latency, default=0.0)
    return (
        f"튜터 요청 지연(초, 동시 {metrics.WORKERS}): p50 {p50:.2f} · p95 {p95:.2f}"
        f" · 최대 {top:.2f} (요청 {len(latency)}개)\n"
    )


def _token_line(buckets: Mapping[tuple[str, int], int]) -> str:
    """성공한 시도의 completion_tokens 분포 한 줄(역할별 p50·p95·최대 구간). 없으면 빈 문자열.

    구간은 TOKEN_BUCKET 단위로 올린 값이라, 상한을 최대 구간까지만 낮추면 성공한 시도는 잘리지
    않았을 것이다.
    """
    parts = []
    for role, hist in _by_role(buckets).items():
        values = [bucket for bucket, n in hist.items() for _ in range(n)]
        p50, p95 = _pct(values, 0.5), _pct(values, 0.95)
        parts.append(f"{role} p50 {p50:.0f} · p95 {p95:.0f} · 최대 {max(values)} ({len(values)}회)")
    if not parts:
        return ""
    head = f"성공한 시도의 completion_tokens(추론 포함, {TOKEN_BUCKET} 단위로 올린 구간)"
    return f"{head}: {', '.join(parts)}\n"


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
    ap.add_argument(
        "--max-calls", type=int, help="운영진 API 호출 상한(기본: config/tutor.yaml eval.max_calls)"
    )
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
    """--tutor-set을 읽어 튜터 설정을 만든다. tutor 프리셋은 운영진 API 환경변수를 본다."""
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
    except OSError as exc:  # 설정 파일이 없거나 못 읽음
        ap.error(f"튜터 설정 파일을 읽지 못했다: {type(exc).__name__}")
    except ValueError as exc:  # pydantic ValidationError도 ValueError
        ap.error(f"--tutor-set 설정이 맞지 않다: {type(exc).__name__}")
    if args.preset == "tutor":  # 두 모드 모두 부른다. 없으면 모든 응답이 기본값으로 떨어진다
        missing = missing_api_env()
        if missing:
            ap.error(f"mode {cfg.mode}는 운영진 API를 부른다: {'·'.join(missing)}이 필요하다")
    return cfg, overrides


def _own_state(cfg: TutorConfig, overrides: dict[str, Any]) -> bool:
    """tutor 프리셋이 handle의 기본 state 대신 자기 state를 만들어 재는가.

    설정을 덮었거나 임베딩을 켰을 때다(기본 state는 기동 때 임베딩을 만들지 못한다).
    """
    return bool(overrides) or cfg.toggles.embedding


def _tutor_state(
    cfg: TutorConfig, overrides: dict[str, Any], cap: int
) -> tuple[Any, CountedEmbed | None, tuple[int, int]]:
    """tutor 프리셋이 쓸 기동 결과, 임베딩을 켰으면 그 호출을 세는 함수, 기동 직후 (호출, 실패).

    튜터 LLM 호출에 상한을 건다: 예상 수 확인은 시작 전에 한 번이고, 이것은 실행 중의 멈춤이다.
    임베딩을 켰으면 서버 기동과 같은 함수(startup.build_embedding)로 KB 임베딩을 만든다.
    """
    if _own_state(cfg, overrides):
        state = SimpleNamespace()
        prepare(state, cfg)
    else:
        state = pipeline.default_state()
    state.tutor_llm.max_calls = cap
    if not cfg.toggles.embedding:
        return state, None, (0, 0)
    counted = CountedEmbed(openai_embed())
    asyncio.run(build_embedding(state, counted))
    return state, counted, (counted.calls, counted.failures)


@dataclass(frozen=True)
class EmbeddingUse:
    """측정 중 임베딩 사용. 기동 빌드와 그 뒤 질의를 나눠 센다.

    queries는 영어 질의 × 실행, 곧 임베딩을 쓸 것으로 예상한 질의 수다. 검색 전에 끝나는 행이
    있을 수 있어 정확한 값은 아니다.
    """

    build: int  # 기동 때 KB 묶음 호출
    calls: int  # 기동 뒤 질의 임베딩 호출
    failures: int  # 기동 뒤 실패(오류·시간 초과·쓸 수 없는 벡터: 그 질의는 BM25)
    queries: int

    @property
    def ok(self) -> bool:
        """임베딩 효과로 읽어도 되는가: 질의 실패가 없고, 영어 질의가 있으면 한 번은 썼다."""
        return not self.failures and not (self.queries and self.calls == 0)


def _embedding_use(
    counted: CountedEmbed | None, built: tuple[int, int], rows: Sequence[Any], runs: int
) -> EmbeddingUse | None:
    if counted is None:
        return None
    calls, failures = counted.calls - built[0], counted.failures - built[1]
    return EmbeddingUse(built[0], calls, failures, english_queries(rows) * runs)


def embedding_summary(use: EmbeddingUse, out: TextIO) -> None:
    """임베딩 호출 수를 내고, 결과를 임베딩 효과로 읽으면 안 되는 까닭이 있으면 알린다."""
    out.write(f"임베딩 호출 {use.build + use.calls}회(기동 {use.build}회 + 질의 {use.calls}회")
    out.write(f" / 영어 질의 {use.queries}회, 실패 {use.failures}회)\n")
    if use.failures:
        out.write("일부 질의 임베딩이 실패해 BM25로 떨어졌다")
        out.write(": 이 결과를 임베딩 효과로 읽지 않는다\n")
    if use.queries and use.calls == 0:
        out.write("질의 임베딩이 한 번도 쓰이지 않았다(이 모드는 검색에 임베딩을 쓰지 않는다)")
        out.write(": 이 결과는 BM25 수치다\n")
    elif 0 < use.calls < use.queries:
        out.write(f"영어 질의 {use.queries}회 중 {use.calls}회만 임베딩을 썼다")
        out.write("(검색 전에 끝난 행이나 멈춘 뒤 막은 질의가 있을 수 있다)\n")
    elif use.calls > use.queries:
        out.write(f"질의 임베딩이 예상({use.queries}회)보다 많다: 예상 호출 수 계산을 다시 본다\n")


def _inputs(ap: argparse.ArgumentParser, args: argparse.Namespace) -> tuple[Any, ...]:
    """API를 부르기 전에 입력을 모두 읽고 검사한다.

    돌려주는 것: (행, 저장 응답, 판정기, 비교 점수, 튜터 설정, 덮은 설정, 임베딩 예상 호출).
    """
    if args.runs < 1:
        ap.error("--runs는 1 이상이어야 한다")
    cfg, overrides = _tutor_config(ap, args)
    if args.max_calls is None:
        args.max_calls = cfg.eval.max_calls
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
    embedding = 0
    if args.preset == "tutor" and cfg.toggles.embedding:
        if cfg.mode == "baseline":  # 기동 호출을 헛되게 쓰지 않는다
            ap.error("베이스라인은 정의상 BM25만 쓴다(TU-14): 임베딩을 켜고 재지 않는다")
        missing = missing_embedding_env()
        if missing:  # 주소가 비면 SDK 기본 주소(공개 OpenAI)로 나간다
            ap.error(f"임베딩을 켜면 {'·'.join(missing)}이 필요하다(런박스)")
        embedding = expected_embedding_calls(rows, runs=args.runs)
        sys.stdout.write(f"임베딩 예상 호출 {embedding}회(기동 KB 묶음 + 실행마다 영어 질의)\n")
    if calls + embedding > args.max_calls:
        ap.error(f"예상 호출 {calls + embedding}회가 상한 {args.max_calls}회를 넘는다")
    return rows, fixed, judge, previous, cfg, overrides, embedding


def main(argv: Sequence[str] | None = None) -> int:
    ap = _parser()
    args = ap.parse_args(argv)
    rows, fixed, judge, previous, cfg, overrides, embedding = _inputs(ap, args)
    reserve = expected_calls(rows, preset=None, runs=args.runs, judge=args.judge)  # 판정 몫
    tutor_cap = args.max_calls - reserve - embedding
    stop = RateLimitStop()  # 튜터 LLM·판정이 같이 쓴다(같은 팀 키)
    state, counted, built = None, None, (0, 0)
    if args.preset == "tutor":
        state, counted, built = _tutor_state(cfg, overrides, tutor_cap)
        stop.watch_llm(state.tutor_llm)
    if counted is not None and state.tutor_emb is None:
        ap.error("임베딩을 켰지만 KB 임베딩을 만들지 못했다(이대로 재면 BM25 수치다)")
    if counted is not None:  # KB 임베딩(기동)은 이미 끝났다. 질의 임베딩만 막는다
        state.tutor_emb = stop.watch_embed(state.tutor_emb)
    latency: list[float] = []
    if fixed is None:  # 자기 state가 아니면 tutor.handle(기본 state)로 부른다
        own = state if _own_state(cfg, overrides) else None
        all_responses, latency = collect_timed(args.preset, rows, args.runs, own)
    else:
        all_responses = [fixed] * args.runs
    llm_stopped = stop.tripped  # 튜터 단계에서 멈췄나(판정 단계에서 멈춘 것과 가른다)
    stop.next_stage()
    kb = score_local.load_kb_dir(KB_DIR)
    scorer = stop.watch_judge(judge) if judge is not None else None
    report = metrics.build_report(
        [metrics.score_run(rows, responses, judge=scorer, kb=kb) for responses in all_responses]
    )
    blocked = sum(state.tutor_llm.blocked.values()) if state is not None else 0
    failed = sum(state.tutor_llm.failures.values()) if state is not None else 0
    use = _embedding_use(counted, built, rows, args.runs)
    tainted = bool(blocked) or bool(failed) or (use is not None and not use.ok) or stop.tripped
    invalid = stop.reason()
    name = args.preset or args.responses.stem
    path = _write(
        report,
        name=name,
        judge=judge is not None,
        out_dir=args.out,
        tainted=tainted,
        embedding=use,
        invalid=invalid,
        llm=state.tutor_llm if state is not None else None,
    )
    _show(report, sys.stdout)
    if state is not None:
        cap = (tutor_cap, args.max_calls, reserve, embedding)
        _show_tutor_calls(state, blocked, latency, cap=cap, stopped=llm_stopped)
    if use is not None:
        embedding_summary(use, sys.stdout)
    if previous is not None and invalid is None:  # 멈춘 실행은 기본값이 섞여 비교하지 않는다
        lost = metrics.regressions(previous, report)
        sys.stdout.write(f"새로 틀린 문항 {len(lost)}개{_ids(lost)}\n")
    sys.stdout.write(f"저장: {path}\n")
    if invalid is not None:
        sys.stderr.write(f"{invalid}. 결과는 무효다\n")
        return EXIT_RATE_LIMITED
    return 1 if tainted else 0


def _show_tutor_calls(
    state: Any,
    blocked: int,
    latency: Sequence[float],
    *,
    cap: tuple[int, int, int, int],
    stopped: bool,
) -> None:
    """튜터가 보낸 LLM 호출 수(모든 실행 합, TU-16), 요청 지연, 실패한 호출, 상한에 막힌 호출 수.

    실패한 시도의 갈래(시도 단위)와 성공한 시도의 completion_tokens 분포도 낸다. 둘 다 위
    "호출"(보낸 시도 수)과 같은 단위라, max_completion_tokens를 정할 때 length·timeout 비율과
    토큰 분포를 함께 본다(본문은 내지 않는다).
    cap은 (튜터 상한, --max-calls, 판정 몫, 임베딩 몫)이다. stopped면 속도 제한으로 멈춘 실행이라
    막힌 호출을 상한이 아니라 속도 제한 몫으로 낸다(멈출 때 상한을 0으로 내린다).
    """
    calls = state.tutor_llm.calls
    shown = ", ".join(f"{role} {n}회" for role, n in sorted(calls.items())) or "0회"
    sys.stdout.write(f"튜터 LLM 호출: {shown}\n")
    sys.stdout.write(_latency_line(latency))
    failures = state.tutor_llm.failures
    if failures:  # 최종 실패한 호출의 답은 기본값이다(연결 안 됨·키 거절·시간 초과 등)
        # 호출 단위(재시도 뒤 최종 오류)라 위 "호출"(보낸 시도 수)과 단위가 다르다
        failed = ", ".join(f"{role} {code} {n}회" for (role, code), n in sorted(failures.items()))
        sys.stdout.write(f"튜터 LLM 최종 실패: {failed}: 기본값이 섞인 결과다\n")
    errors = state.tutor_llm.attempt_errors
    if errors:  # bad_json은 원인 갈래(length 등), 그 밖은 오류 코드(timeout 등)
        shown = ", ".join(f"{role} {label} {n}회" for (role, label), n in sorted(errors.items()))
        sys.stdout.write(f"실패한 시도(갈래별, 재시도 포함): {shown}\n")
    sys.stdout.write(_token_line(state.tutor_llm.completion_buckets))
    if blocked and stopped:
        sys.stdout.write(f"속도 제한(429)으로 멈춰 보내지 못한 튜터 호출 {blocked}회")
        sys.stdout.write(": 기본값이 섞인 결과다\n")
    elif blocked:  # 막힌 요청은 기본값으로 채점됐다. 이 점수를 측정값으로 쓰지 않는다
        tutor_cap, max_calls, reserve, embedding = cap
        sys.stdout.write(f"상한에 막혀 보내지 못한 튜터 호출 {blocked}회(튜터 상한 {tutor_cap}회")
        sys.stdout.write(f" = --max-calls {max_calls} - 판정 몫 {reserve}")
        sys.stdout.write(f" - 임베딩 몫 {embedding}" if embedding else "")
        sys.stdout.write("): 기본값이 섞인 결과다\n")


if __name__ == "__main__":
    raise SystemExit(main())
