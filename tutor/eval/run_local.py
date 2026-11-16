"""로컬 측정(TU-16·T1.5). 저장소 루트에서 `python -m tutor.eval.run_local --preset tutor`.

프리셋
- tutor: 지금 튜터(`tutor.handle`)를 프로세스 안에서 부른다(동시 8개). 라우터·인증·HTTP는
  거치지 않는다. 서버 경로 측정은 통합 하네스가 한다. `--tutor-set KEY=VALUE`로 설정을 덮는다
  (예: `--tutor-set mode=baseline`은 베이스라인). v2(주 호출)도 베이스라인도 운영진 API를
  부르므로 OPENAI_BASE_URL·OPENAI_MODEL·OPENAI_API_KEY가 모두 있어야 한다
- reference: 공개 정답 답변·action·근거를 그대로 낸다(상한, PRD §7)
- reference_top1: 정답 답변·action에, 근거가 있는 행은 검색 1위 섹션을 인용한다
`--responses` 파일(JSONL, 줄마다 {"id", "response"})을 주면 그 응답(통합 하네스 결과 등)으로
파일에 있는 행만 잰다(홀드아웃 행은 아래처럼 뺀다). `--cases team`이면 공개 세트 대신 팀 작성
점검 세트(case_set, 홀드아웃 없음)를 재고 축별 action 정확도·응함을 더 낸다(TU-19).
`--cases <경로>`가 공개 튜터 행만 담은 파일(target_set)이면 그 행만 공개 세트처럼 잰다(결과 subset).

판정(사실·충실·응함)은 `--judge`를 줄 때만 켠다. 운영진 API를 부르므로 OPENAI_BASE_URL·
OPENAI_MODEL·OPENAI_API_KEY가 모두 있어야 한다(런박스). 없으면 action·인용만 채점한다.
실행 전에 운영진 API 예상 호출 수(재시도 여유 1.1배, 가드 하네스와 같은 방식)를 출력하고
`--max-calls`(기본은 config/tutor.yaml의 eval.max_calls, TU-14)를 넘으면
아무것도 부르지 않고 멈춘다. 실행 중에는 판정 몫(예상 판정 호출)을 뺀 나머지를 튜터 LLM 호출
상한으로 건다.
튜터 LLM 호출의 429(판정은 최종 실패)가 연달아 RATE_LIMIT_STREAK회 이어지면 뒤의 호출(임베딩
포함)을 보내지 않고, 결과를 무효(invalid)로 남기고 종료 코드 EXIT_RATE_LIMITED로 끝난다(가드
하네스와 같은 규칙).
공유 분할(가드 splits/v1.json)의 홀드아웃 묶음 행(공개 튜터 40행)은 재지 않는다. 모든 프리셋과
--responses에서 예상 호출 수를 세기 전, 튜터·판정·임베딩을 부르기 전에 뺀다. 홀드아웃은 설정을
고정한 뒤 통합 하네스(#61)에서 최대 3회만 보므로(TU-16·#84) 넣는 옵션은 두지 않는다. 그래서 모든
지표가 교차 검증 행 값이고, 교차 검증 행 줄(설정 비교용)은 첫 줄 점수와 같다. 뺀 행 수는 결과
JSON의 holdout_excluded에 남는다. 홀드아웃 행이 섞인 이전 결과(200행)와도 --compare할 수 있다:
이번에 재지 않은 행은 새로 틀린 문항으로 세지 않는다.
결과는 tutor/eval/runs/에 JSON으로 남는다(git 제외). 질문·답변 본문은 출력하지 않는다.
결과 JSON의 report.cv_emitted_by_run에는 실행마다 행 id → 낸 action·인용 수·인용한 섹션 ID(KB에
있는 것만)·응함 판정·사실 점수·충실 판정을 남긴다(형식이 틀린 응답이면 모두 null, 판정하지
않았거나 판정 오류이거나 그 문항에 해당 항목이 없으면 응함·사실·충실이 null, 본문은 남기지
않는다, TU-15). 사실 점수를 어느 행에서 잃는지 행별로 볼 수 있다. 화면에는 정답이 abstain·refuse인데
answer를 낸 행 id와 그중 인용 0인 수를 실행별로 낸다(#162·#163). 이 필드가 없는 이전 결과와도
--compare할 수 있다.
프리셋 실행 결과 JSON의 guard_config에는 튜터 검색에 닿는 가드 설정(읽은 파일, KB 폴더, kb
ngram·k1·b, retrieval.max_query_chars, config_hash)을 남기고, 설정 파일(GUARD_CONFIG)이나 KB
폴더(GUARD_KB_DIR)가 저장소 기본과 다르면 API를 부르기 전에 화면에 무엇이 다른지 한 줄 낸다
(--responses는 이 프로세스가 답을 만들지 않아 남기지 않는다). 이 필드가 없는 이전 결과와도
--compare할 수 있다.
tutor 프리셋은 튜터 LLM 수(보낸 시도·최종 실패·실패한 시도 갈래·성공한 시도의 토큰 구간)도
결과 JSON의 tutor_llm에 남긴다.
근거 대조 칸(`toggles.evidence_check`, #162)을 켜면 행마다 주 호출이 쓴 칸 값을 결과 JSON의
evidence_check에 남기고, 정답 범주별 분포와 "칸 false인 answer를 abstain으로 바꾸면" 모의 수를 낸다.
"""

import argparse
import asyncio
import hashlib
import json
import math
import os
import secrets
import sys
import threading
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TextIO

import yaml
from guardrail.config import DEFAULT_CONFIG as GUARD_DEFAULT_CONFIG
from guardrail.config import config_hash, resolve_path
from guardrail.config import load_config as load_guard_config
from guardrail.data import labeled_rows, load_public
from guardrail.eval.cv import load_splits, train_rows
from guardrail.eval.run import probe_calls as guard_probe_calls
from guardrail.guard import Guard
from guardrail.llm import LLM

import tutor
from tutor import pipeline
from tutor.eval import case_set, gate, metrics
from tutor.eval.grader import KB_DIR, PUBLIC_DIR, judge_from_env, score_local, scoring
from tutor.eval.recall import (
    CountedEmbed,
    english_queries,
    expected_embedding_calls,
    missing_embedding_env,
    query_parts,
)
from tutor.judge.examples import build_index, load_examples
from tutor.judge.main_call import EVIDENCE_CHECK_PROBE
from tutor.retrieval import retrieve
from tutor.retrieval.embedding import openai_embed
from tutor.runtime.config import PROXY_MAX_COMPLETION_TOKENS, ROOT, TutorConfig, load_config
from tutor.runtime.llm import TOKEN_BUCKET, missing_api_env
from tutor.startup import build_embedding, prepare

PRESETS = ("tutor", "reference", "reference_top1")
RUNS_DIR = Path(__file__).resolve().parent / "runs"
# 튜터 요청당 nano 정상 호출 수(v2 최종 구성 = 주 호출 + 가드 classify + judge_output, TU-14).
# 지금 v2는 주 호출만 해서(#97, classify·judge_output은 #88) 예상이 실제보다 크다(상한 쪽이라 안전).
TUTOR_CALLS = 3
CALLS_PER_REQUEST = {"v2": TUTOR_CALLS, "baseline": 1}  # 모드별 요청당 nano 호출(재시도 제외)
SHOW_IDS = 30  # 화면에 낼 행 id 수(전체는 결과 JSON에 있다)
# --guard: 요청당 가드 호출(classify + judge_output, TU-13). TUTOR_CALLS 안에서 나눠 쓴다.
# 가드 LLM 기동 시험(probe) 호출 수는 가드 설정에서 센다(guard_probe_calls)
GUARD_CALLS_PER_REQUEST = 2
# --guard 결과를 비교 기준에서 빼는 가드 단계 건너뜀 사유: 가드가 서버와 다르게 답한 것이다.
# timeout·no_time은 시간 제한이 설계대로 작동한 것이라 ⓐ·ⓒ·검증 켬에서 재려는 효과 자체다.
# 수로만 남긴다(#165 리뷰 danielcho02 2차, mock_load.BAD_SKIPS와 같은 판정)
GUARD_BAD_SKIPS = frozenset({"missing", "error", "invalid"})
# 운영진 API 호출이 이만큼 연달아 429로 끝나면(각각 재시도 포함) 더 보내지 않는다. 막힌 키에 계속
# 보내면 어뷰징으로 보일 수 있다(SPEC §7.1). 값과 종료 코드는 가드 하네스(#151)와 같다
RATE_LIMIT_STREAK = 20
EXIT_RATE_LIMITED = 6


def public_tutor_rows() -> list[dict[str, Any]]:
    lines = (PUBLIC_DIR / "tutor.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def holdout_ids(rows: Sequence[dict[str, Any]] = ()) -> frozenset[str]:
    """공유 분할(가드 splits/v1.json)의 홀드아웃 묶음에 든 행 id. 잴 행에서 뺀다(TU-16·#84).

    rows를 주면 모두 분할에 있는지 본다. 없는 행이 조용히 교차 검증 행으로 들어가지 않게 한다.
    """
    split = load_splits()
    missing = sum(1 for row in rows if row["id"] not in split.cluster)
    if missing:
        raise ValueError(f"공유 분할에 없는 행 {missing}개: 교차 검증 점수를 낼 수 없다")
    held = set(split.holdout)
    return frozenset(row_id for row_id, cluster in split.cluster.items() if cluster in held)


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
    preset: str,
    rows: Sequence[dict[str, Any]],
    state: Any,
    checks: dict[str, bool | None] | None = None,
    calls_log: list[int] | None = None,
) -> tuple[dict[str, Any], list[float]]:
    """한 실행의 (행 id → 응답, 요청마다 걸린 시간(초)). 실패한 요청도 시간을 잰다.

    calls_log가 있으면 요청마다 보낸 nano 시도 수(튜터·가드, `gate.track`한 LLM)를 덧붙인다.

    checks가 있으면 행마다 주 호출이 쓴 근거 대조 칸 값(#162)을 담는다. 주 호출이 칸을 내지
    않았으면(주 호출 실패, 주 호출 전에 끝난 요청, 칸 끔) None이다.
    """
    slots = asyncio.Semaphore(metrics.WORKERS)

    async def one(row: dict[str, Any]) -> tuple[str, Any, float]:
        async with slots:
            started = time.perf_counter()
            own = state.for_row(row["id"]) if isinstance(state, FoldStates) else state
            seen: list[bool] = []  # 이 행(요청) 하나의 컨텍스트에만 있다
            token = EVIDENCE_CHECK_PROBE.set(seen if checks is not None else None)
            sent: Counter[str] = Counter()  # 이 요청이 보낸 호출(재시도 포함)
            sent_token = gate.REQUEST_CALLS.set(sent)
            try:
                response = await _respond(preset, row, own)
            except Exception as exc:  # 로컬 채점기처럼 실패한 호출은 응답 없음(형식 오류)
                sys.stderr.write(f"{row['id']}: {type(exc).__name__}\n")  # 메시지는 내지 않는다
                response = None
            finally:
                EVIDENCE_CHECK_PROBE.reset(token)
                gate.REQUEST_CALLS.reset(sent_token)
            if calls_log is not None:
                calls_log.append(sum(sent.values()))
            if checks is not None:
                checks[row["id"]] = seen[-1] if seen else None
            return row["id"], response, time.perf_counter() - started

    done = await asyncio.gather(*(one(row) for row in rows))
    return {row_id: response for row_id, response, _took in done}, [t for _i, _r, t in done]


def collect_timed(
    preset: str,
    rows: Sequence[dict[str, Any]],
    runs: int,
    state: Any = None,
    checks: list[dict[str, bool | None]] | None = None,
    *,
    setup: Callable[[], Awaitable[Any]] | None = None,
    calls_log: list[int] | None = None,
) -> tuple[list[dict[str, Any]], list[float]]:
    """실행마다 응답을, 그리고 요청마다 걸린 시간(초, 모든 실행)을 모은다(TU-16 지연).

    튜터가 루프에 묶인 클라이언트를 써도 되게 루프 하나에서 돈다. 동시 요청은 metrics.WORKERS개다.
    state가 없으면 tutor 프리셋은 tutor.handle(기본 설정)로, 있으면 그 state로 부른다.
    setup이 있으면 같은 루프에서 먼저 불러 그 결과를 state로 쓴다(가드 LLM 클라이언트도 루프에
    묶인다, --guard).
    checks(목록)를 주면 실행마다 행 → 근거 대조 칸 값(#162)을 하나씩 덧붙인다.
    """
    if preset not in PRESETS:
        raise ValueError(f"모르는 프리셋: {preset}")

    async def every_run() -> list[tuple[dict[str, Any], list[float]]]:
        own = await setup() if setup is not None else state
        out = []
        for _ in range(runs):
            seen: dict[str, bool | None] | None = None if checks is None else {}
            out.append(await _collect(preset, rows, own, seen, calls_log))
            if checks is not None and seen is not None:
                checks.append(seen)
        return out

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


def load_previous(
    path: Path, *, judge: bool, cases: str = "public"
) -> dict[str, list[float | None]]:
    """--compare 결과 파일의 행별 문항 점수. 판정 모드나 잰 세트가 다르면 비교하지 않는다."""
    data = json.loads(path.read_text(encoding="utf-8"))
    report = data.get("report") if isinstance(data, dict) else None
    points = report.get("points") if isinstance(report, dict) else None
    if not isinstance(points, dict):
        raise ValueError(f"비교 파일에 report.points가 없다: {path}")
    if data.get("judge") is not judge:
        raise ValueError("판정 모드(--judge)가 다른 결과와는 비교하지 않는다")
    if data.get("cases", "public") != cases:
        raise ValueError("공개 세트와 점검 세트(--cases) 결과는 서로 비교하지 않는다")
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
        self._llms: list[Any] = []  # watch_llm으로 감싼 LLM(튜터·가드). 멈추면 모두 막는다

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
        """튜터 LLM·가드 LLM의 complete를 감싼다(서버·가드 코드는 그대로다). 둘 다 같은 팀 키다.

        멈추면 감싼 LLM 모두의 max_calls를 0으로 내린다. 어느 쪽 429로 멈췄든 뒤의 호출과 진행 중인
        재시도는 보내지 않고 budget이 된다.
        """
        inner = llm.complete
        self._llms.append(llm)

        async def complete(*args: Any, **kwargs: Any) -> Any:
            result = await inner(*args, **kwargs)
            if self.record(result.error):
                for watched in self._llms:
                    watched.max_calls = 0
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
    holdout_excluded: int,
    guard: dict[str, Any] | None = None,
    guard_run: Mapping[str, Any] | None = None,
    llm: Any = None,
    checks: Sequence[Mapping[str, bool | None]] | None = None,
    check_summary: "EvidenceCheckSummary | None" = None,
    team: Mapping[str, Any] | None = None,
    gate_summary: Mapping[str, Any] | None = None,
    tutor: Mapping[str, Any] | None = None,
    subset: Mapping[str, Any] | None = None,
) -> Path:
    """결과 JSON을 남긴다. tainted면 기본값이나 BM25가 섞인 결과라 --compare 기준이 되지 못한다.

    invalid는 실행을 멈춘 사유다(연속 429·판정 최종 실패). 있으면 tainted도 참이다.
    holdout_excluded는 재지 않고 뺀 홀드아웃 행 수다(행 id는 남기지 않는다).
    guard는 guard_config_used()의 값이다(없으면 남기지 않는다).
    llm(튜터 LLM)이 있으면 그 수를 tutor_llm에 남긴다(본문 없음). load_previous는 이 필드를
    읽지 않아, 필드가 없는 옛 결과도 --compare에 쓴다. team(점검 세트)이면 cases=team도 남긴다.
    checks가 있으면(근거 대조 칸을 켠 측정, #162) 실행마다 행 → 칸 값과 그 진단을 남긴다.
    tutor가 있으면(tutor 프리셋) 적용된 mode·토글과 --tutor-set 값을 남겨 비교군 조건을 확인한다.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = out_dir / f"{name}-{stamp}.json"
    data: dict[str, Any] = {"preset": name, "judge": judge, "created": stamp, "tainted": tainted}
    data["holdout_excluded"] = holdout_excluded
    if guard is not None:
        data["guard_config"] = guard
    if tutor is not None:
        data["tutor_config"] = dict(tutor)
    if guard_run is not None:  # --guard: 가드 호출·오류·건너뜀 수(본문 없음)
        data["guard_run"] = dict(guard_run)
    if gate_summary is not None:  # #83 관문 조건(응함·재현율·지연·요청당 nano 호출)
        data["gate"] = dict(gate_summary)
    if invalid is not None:
        data["invalid"] = invalid
    if checks is not None:
        data["evidence_check"] = {
            "by_run": [dict(sorted(seen.items())) for seen in checks],
            "summary": asdict(check_summary) if check_summary is not None else None,
        }
    if team is not None:
        data["cases"], data["team"] = "team", dict(team)
    if subset is not None:  # 공개 부분 세트: 파일 이름·잰 행 수·id 묶음 해시(전체 점수와 다른 세트)
        data["subset"] = dict(subset)
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


def _shown_path(path: Path) -> str:
    """저장소 안이면 저장소 루트 기준 상대 경로, 밖이면 절대 경로."""
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def guard_config_used() -> tuple[dict[str, Any], list[str]]:
    """튜터에 닿는 가드 설정(숫자·경로만)과, 저장소 기본과 다른 것(설정 파일·KB 폴더, 값과 함께).

    서버와 같은 로더(가드 load_config, GUARD_CONFIG·GUARD_KB_DIR를 따른다)로 읽는다. #158부터 튜터
    검색이 KB 폴더(paths.kb), kb(ngram·k1·b), retrieval.max_query_chars를 이 설정에서 읽는다.
    GUARD_KB_DIR는 설정 파일을 그대로 두고 KB 폴더만 바꾸므로 따로 본다(#168 리뷰). 기본 KB
    폴더는 저장소 기본 설정 파일이 적은 paths.kb다.
    """
    used = resolve_path(os.environ.get("GUARD_CONFIG", GUARD_DEFAULT_CONFIG)).resolve()
    cfg = load_guard_config()
    kb_dir = resolve_path(cfg.paths.kb).resolve()
    guard = {
        "path": _shown_path(used),
        "kb_dir": _shown_path(kb_dir),
        "kb": {"ngram": cfg.kb.ngram, "k1": cfg.kb.k1, "b": cfg.kb.b},
        "max_query_chars": cfg.retrieval.max_query_chars,
        "config_hash": config_hash(cfg),
    }
    default_file = resolve_path(GUARD_DEFAULT_CONFIG)
    default_kb = yaml.safe_load(default_file.read_text(encoding="utf-8"))["paths"]["kb"]
    changed = []
    if used != default_file.resolve():
        changed.append(f"설정 파일 {guard['path']}")
    if kb_dir != resolve_path(default_kb).resolve():
        changed.append(f"KB 폴더 {guard['kb_dir']}")
    return guard, changed


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


def _per_run(values: Sequence[float | None], spec: str) -> str:
    """실행별 값과 범위. 값이 없는 실행(None)은 -로 내고 범위에서 뺀다."""
    shown = ", ".join("-" if v is None else format(v, spec) for v in values)
    present = [v for v in values if v is not None]
    if not present:
        return f"{shown} (범위 -)"
    return f"{shown} (범위 {min(present):{spec}}~{max(present):{spec}})"


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
    # 상한 구간은 그보다 큰 값도 담고, usage가 없는 시도는 빠진다(괄호 안 횟수는 구간에 든 수)
    head = (
        f"성공한 시도의 completion_tokens(추론 포함, {TOKEN_BUCKET} 단위로 올린 구간"
        f", {PROXY_MAX_COMPLETION_TOKENS}은 초과 포함, 토큰 수 없는 시도 제외)"
    )
    return f"{head}: {', '.join(parts)}\n"


def _show(report: metrics.Report, out: TextIO, *, judged: bool) -> None:
    scores = ", ".join(f"{t:.2f}" for t in report.tutor)
    out.write(f"튜터 점수(실행별): {scores}, 범위 {min(report.tutor):.2f}~{max(report.tutor):.2f}")
    out.write(f" (문항 {report.rows}개, 실행 {report.runs}회")
    out.write(f", 판정 오류 {sum(report.judge_errors)})\n")
    # 교차 검증 행이 없으면 0.00이 아니라 -로 낸다(재서 0인 것처럼 읽히지 않게)
    cv_score = _per_run(report.cv_tutor, ".2f") if report.cv_rows else "- (측정 없음)"
    out.write(f"교차 검증 행 튜터 점수(실행별, 설정 비교용): {cv_score}")
    out.write(f", 문항 {report.cv_rows}개")
    out.write(f", 판정 오류 {sum(report.cv_judge_errors)}\n")
    parts = ", ".join(
        f"{name} {'-' if value is None else f'{value:.3f}'}"
        for name, value in report.components.items()
    )
    out.write(f"항목별 평균: {parts}, 응함 {report.complied}\n")
    out.write("교차 검증 행 항목별(실행별):\n")
    for name in report.components:
        values = [run[name] for run in report.cv_components_by_run]
        out.write(f"- {name} {_per_run(values, '.3f')}\n")
    if not judged:  # 응함은 판정이 있어야 잰다. 0으로 내면 재서 0인 것처럼 읽힌다
        out.write("- 응함 - (판정 없음)\n")
        out.write("교차 검증 행에서 응함으로 잡힌 문항(실행별): 판정 없음\n")
    else:
        out.write(f"- 응함 {_per_run([len(ids) for ids in report.cv_complied_by_run], 'd')}\n")
        out.write("교차 검증 행에서 응함으로 잡힌 문항(실행별):\n")
        for number, ids in enumerate(report.cv_complied_by_run, 1):
            out.write(f"- {number}회 {len(ids)}개{_ids(ids)}\n")
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
    # 인용 0인 answer는 claim이 모두 지워진 answer 경로 후보다(#162 가설 3)
    for gold, by_run in report.cv_answered_by_run.items():
        out.write(f"정답 {gold} → 낸 answer인 행(실행별, 괄호는 그중 인용 0인 수):\n")
        runs = zip(by_run, report.cv_emitted_by_run, strict=True)
        for number, (ids, emitted) in enumerate(runs, 1):
            bare = sum(emitted[i]["citations"] == 0 for i in ids)
            out.write(f"- {number}회 {len(ids)}개(인용 0 {bare}개){_ids(ids)}\n")


def _check_key(value: bool | None) -> str:
    return "none" if value is None else ("true" if value else "false")


@dataclass(frozen=True)
class EvidenceCheckSummary:
    """근거 대조 칸(`toggles.evidence_check`, #162) 진단. 모든 실행을 합친 응답 수다.

    칸이 범주 판단을 못 바꾼 까닭을 가른다: out_of_material 행에 true를 쓰면 대조 자체가 안 되고,
    false를 쓰고도 answer를 내면 칸과 범주가 어긋난다. 뒤의 경우는 "칸 false인 answer를 abstain으로
    바꾸는" 결정적 처리를 모의로 센다(바꾸지는 않는다).
    """

    by_category: dict[str, dict[str, int]]  # 정답 범주 → {"true"·"false"·"none" → 수}
    abstain_answered: dict[str, int]  # 정답 abstain에 answer를 낸 응답의 칸 값 분포
    flip_gain: int  # 칸 false인 answer 중 정답 abstain(바꾸면 맞게 바뀜)
    flip_loss: dict[str, int]  # 칸 false인 answer 중 정답이 abstain이 아닌 것: 정답 action → 수


def evidence_check_summary(
    rows: Sequence[dict[str, Any]],
    all_responses: Sequence[Mapping[str, Any]],
    checks: Sequence[Mapping[str, bool | None]],
) -> EvidenceCheckSummary:
    by_category: dict[str, dict[str, int]] = {}
    answered: dict[str, int] = {}
    gain, loss = 0, {}
    for responses, seen in zip(all_responses, checks, strict=True):
        for row in rows:
            labels = row["labels"]
            value = seen.get(row["id"])
            key = _check_key(value)
            counts = by_category.setdefault(labels["category"], {})
            counts[key] = counts.get(key, 0) + 1
            response = responses.get(row["id"])
            action = response.get("action") if isinstance(response, dict) else None
            if action != "answer":
                continue
            if labels["action"] == "abstain":
                answered[key] = answered.get(key, 0) + 1
            if value is False:
                if labels["action"] == "abstain":
                    gain += 1
                else:
                    loss[labels["action"]] = loss.get(labels["action"], 0) + 1
    return EvidenceCheckSummary(
        by_category={c: dict(sorted(v.items())) for c, v in sorted(by_category.items())},
        abstain_answered=dict(sorted(answered.items())),
        flip_gain=gain,
        flip_loss=dict(sorted(loss.items())),
    )


def show_evidence_check(summary: EvidenceCheckSummary, out: TextIO) -> None:
    """근거 대조 칸 진단(#162). 행 id·본문 없이 수만 낸다."""

    def triple(counts: Mapping[str, int]) -> str:
        return " / ".join(str(counts.get(k, 0)) for k in ("true", "false", "none"))

    out.write("근거 대조 칸(asked_in_evidence, 모든 실행 합) 정답 범주별 true / false / 없음:\n")
    for category, counts in summary.by_category.items():
        out.write(f"- {category} {triple(counts)}\n")
    out.write("정답 abstain에 answer를 낸 응답의 칸 true / false / 없음: ")
    out.write(f"{triple(summary.abstain_answered)}\n")
    lost = sum(summary.flip_loss.values())
    detail = ", ".join(f"{a} {n}" for a, n in summary.flip_loss.items()) or "없음"
    out.write("칸 false인 answer를 abstain으로 바꾸면(모의, 바꾸지 않음): 정답 abstain ")
    out.write(f"{summary.flip_gain}개가 맞게 바뀌고, 정답이 다른 {lost}개가 바뀐다({detail})\n")


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="튜터 세부 지표 로컬 측정(TU-16·T1.5)")
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--preset", choices=PRESETS)
    source.add_argument("--responses", type=Path, help="JSONL: 줄마다 {id, response}")
    ap.add_argument("--cases", default="public", help="public(기본) | team | 점검 세트 JSONL 경로")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--judge", action="store_true", help="운영진 API로 사실·충실·응함 판정")
    ap.add_argument(
        "--max-calls", type=int, help="운영진 API 호출 상한(기본: config/tutor.yaml eval.max_calls)"
    )
    ap.add_argument("--out", type=Path, default=RUNS_DIR)
    ap.add_argument("--compare", type=Path, help="이전 결과 JSON. 새로 틀린 문항을 낸다")
    ap.add_argument(
        "--guard",
        action="store_true",
        help="가드 판정기(Guard)를 붙여 잰다. 폴드마다 평가 묶음을 뺀 행으로 만든다(TU-10·TU-16)",
    )
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


def _tutor_config_used(cfg: TutorConfig, overrides: Mapping[str, Any]) -> dict[str, Any]:
    """결과 JSON의 tutor_config: 적용된 mode·토글 전부와 --tutor-set으로 덮은 값(본문 없음)."""
    return {
        "mode": cfg.mode,
        "prompt": cfg.prompt.model_dump(),  # 주 호출 프롬프트 파일 이름
        "toggles": cfg.toggles.model_dump(),
        "overrides": dict(sorted(overrides.items())),
    }


def _guard_used(cfg: TutorConfig) -> bool:
    """이 설정이 요청마다 가드를 부르는가(classify 합치기·직렬이나 judge_output 검증, v2만)."""
    toggles = cfg.toggles
    return cfg.mode == "v2" and (toggles.guard_classify != "off" or toggles.verify == "guard")


def _own_state(cfg: TutorConfig, overrides: dict[str, Any], *, guard: bool = False) -> bool:
    """tutor 프리셋이 handle의 기본 state 대신 자기 state를 만들어 재는가.

    설정을 덮었거나 임베딩을 켰을 때다(기본 state는 기동 때 임베딩을 만들지 못한다). 판단 예시를
    켰을 때도다(기본 state의 예시 색인은 200행 전부라 평가 행 자신이 예시로 들어간다). 가드를
    붙일 때도다(기본 state에는 가드가 없다).
    """
    return guard or bool(overrides) or cfg.toggles.embedding or cfg.toggles.fewshot != "off"


@dataclass(frozen=True)
class FoldStates:
    """판단 예시(TU-08)를 켜거나 가드를 붙인 측정: 행마다 그 행의 평가 묶음을 뺀 state.

    폴드 k의 행은 폴드 k와 홀드아웃 묶음을 뺀 예시 색인·가드를, 홀드아웃 행은 홀드아웃 묶음만 뺀
    것을 쓴다(가드 `train_rows`와 같은 규칙, TU-10·TU-16·TD-14). 나머지 기동 결과(LLM 호출기 등)는
    같이 쓴다. `main`은 홀드아웃 행을 재지 않으므로(#84) 홀드아웃 분기는 지금 쓰이지 않는다. 가드
    규칙과 맞춰 두는 방어용이다.
    """

    by_row: Mapping[str, Any]

    def for_row(self, row_id: str) -> Any:
        return self.by_row[row_id]


def _fold_key(split: Any, row_id: str) -> str:
    """행의 평가 묶음: 홀드아웃 묶음이면 "holdout", 아니면 폴드 번호(가드 train_rows와 같다)."""
    cluster = split.cluster[row_id]
    return "holdout" if cluster in set(split.holdout) else str(split.fold[cluster])


def fold_states(
    state: Any,
    cfg: TutorConfig,
    rows: Sequence[dict[str, Any]],
    *,
    guards: Mapping[str, Any] | None = None,
) -> FoldStates:
    """행마다 평가 묶음을 뺀 state. 판단 예시를 켰으면 예시 색인을, guards가 있으면 가드를 바꾼다.

    guards는 build_fold_guards가 만든 (평가 묶음 → 가드)다.
    """
    split = load_splits()
    holdout = set(split.holdout)
    examples = cfg.toggles.fewshot != "off"
    pool = load_examples() if examples else []
    per_key: dict[str, Any] = {}
    for k in sorted({_fold_key(split, row["id"]) for row in rows}):
        extra: dict[str, Any] = {}
        if examples:
            excluded = holdout | (
                set() if k == "holdout" else {c for c, f in split.fold.items() if str(f) == k}
            )
            kept = [e for e in pool if split.cluster[e.id] not in excluded]
            extra["tutor_examples"] = build_index(cfg.fewshot, kept)
        if guards is not None:
            extra["guard"] = guards[k]
        per_key[k] = SimpleNamespace(**{**vars(state), **extra})
    return FoldStates({row["id"]: per_key[_fold_key(split, row["id"])] for row in rows})


async def build_fold_guards(
    rows: Sequence[dict[str, Any]],
    *,
    llm: Any,
    cfg: Any = None,
    build: Callable[..., Awaitable[Any]] | None = None,
) -> dict[str, Any]:
    """평가 묶음마다 그 묶음을 뺀 행으로 가드를 만든다(TU-10·TU-16, #83). 열쇠는 _fold_key와 같다.

    폴드 k 행의 가드는 폴드 k와 홀드아웃 묶음을, 홀드아웃 행의 가드는 홀드아웃 묶음만 뺀 공개
    400행(가드·튜터)으로 kNN·LR을 만든다(가드 train_rows, `Guard.build(rows=…)`). 가드 LLM
    호출기(게이트·probe 결과)는 하나를 같이 쓴다. 튜터 LLM은 자기 게이트를 쓴다(측정 동시 요청은
    metrics.WORKERS개라 게이트가 차지 않는다).
    """
    gcfg = cfg if cfg is not None else load_guard_config()
    split = load_splits()
    labeled = labeled_rows(load_public(gcfg.paths.public))
    make = build if build is not None else Guard.build
    guards: dict[str, Any] = {}
    for k in sorted({_fold_key(split, row["id"]) for row in rows}):
        if k == "holdout":
            train = train_rows(split, labeled, holdout=True)
        else:
            train = train_rows(split, labeled, fold=int(k))
        guards[k] = await make(gcfg, llm=llm, rows=train)
    return guards


def _tutor_state(
    cfg: TutorConfig, overrides: dict[str, Any], cap: int, *, guard: bool = False
) -> tuple[Any, CountedEmbed | None, tuple[int, int]]:
    """tutor 프리셋이 쓸 기동 결과, 임베딩을 켰으면 그 호출을 세는 함수, 기동 직후 (호출, 실패).

    튜터 LLM 호출에 상한을 건다: 예상 수 확인은 시작 전에 한 번이고, 이것은 실행 중의 멈춤이다.
    임베딩을 켰으면 서버 기동과 같은 함수(startup.build_embedding)로 KB 임베딩을 만든다.
    """
    if _own_state(cfg, overrides, guard=guard):
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


def public_subset(path: str) -> dict[str, Any] | None:
    """--cases 파일이 공개 튜터 행만 담았으면 부분 세트 정보(파일 이름·id), 공개 행이 없으면 None.

    None이면 점검 세트로 읽는다(case_set). 공개 행과 다른 행이 섞였거나, id가 겹치거나, 줄이
    공개 파일의 같은 id 행과 다르면 ValueError(행 본문은 메시지에 넣지 않는다).
    """
    public = {row["id"]: row for row in public_tutor_rows()}
    try:
        rows = [json.loads(line) for _n, line in case_set.read_lines(Path(path))]
    except ValueError:
        return None  # 점검 세트 검사가 줄 번호로 알린다
    ids = [row.get("id") for row in rows if isinstance(row, dict) and row.get("id") in public]
    if not ids:
        return None
    if len(ids) != len(rows):
        raise ValueError("--cases 파일에 공개 튜터 행과 다른 행이 섞였다")
    if len(set(ids)) != len(ids):
        raise ValueError("--cases 파일에 같은 공개 행 id가 두 번 있다")
    changed = sum(row != public[row["id"]] for row in rows)
    if changed:
        raise ValueError(f"--cases 파일의 {changed}행이 공개 세트의 같은 id 행과 다르다")
    digest = hashlib.sha256("\n".join(sorted(ids)).encode("utf-8")).hexdigest()[:16]
    return {"file": Path(path).name, "ids": frozenset(ids), "ids_sha256": digest}


def _subset_used(subset: Mapping[str, Any] | None, rows: Sequence[Any]) -> dict[str, Any] | None:
    """결과 JSON의 subset: 파일 이름·id 묶음 해시·잰 행 수(행 id 목록은 남기지 않는다)."""
    if subset is None:
        return None
    return {"file": subset["file"], "ids_sha256": subset["ids_sha256"], "rows": len(rows)}


def _measured_rows(
    ap: argparse.ArgumentParser,
    fixed: Mapping[str, Any] | None,
    subset: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """잴 공개 튜터 행과 뺀 홀드아웃 행 수. 예상 호출 수를 세기 전에 홀드아웃 행을 뺀다.

    홀드아웃은 설정을 고정한 뒤 통합 하네스(#61)에서 최대 3회만 본다(TU-16·#84). 로컬 측정이 돌
    때마다 그 행을 보지 않게, 그리고 운영진 API 호출을 아끼게 모든 프리셋과 --responses에서 뺀다.
    subset(public_subset)이 있으면 그 id의 공개 행만 잰다(행은 공개 파일에서 읽는다).
    """
    public = public_tutor_rows()
    try:
        held = holdout_ids(public)  # 분할에 없는 행이 있으면 API를 부르기 전에 멈춘다
    except ValueError as exc:
        ap.error(str(exc))
    rows = public if fixed is None else [row for row in public if row["id"] in fixed]
    if subset is not None:
        rows = [row for row in rows if row["id"] in subset["ids"]]
    measured = [row for row in rows if row["id"] not in held]
    excluded = len(rows) - len(measured)
    sys.stdout.write(f"홀드아웃 {excluded}행은 재지 않는다(통합 하네스에서 최대 3회, TU-16·#84)\n")
    if subset is not None:
        shown = f"공개 부분 세트 {subset['file']}: 공개 튜터 행 {len(measured)}개를 잰다\n"
        sys.stdout.write(shown)
        if not measured:
            ap.error("공개 부분 세트에 잴 행이 없다(홀드아웃 행은 재지 않는다)")
    if fixed is not None:
        unknown = len(set(fixed) - {row["id"] for row in public})
        sys.stdout.write(f"응답 파일: 공개 튜터 행 {len(measured)}개를 잰다")
        sys.stdout.write(f"(공개 세트에 없는 id {unknown}개는 뺀다)\n")
        if not measured:
            ap.error("응답 파일에 잴 공개 튜터 행 id가 없다(홀드아웃 행은 재지 않는다)")
    return measured, excluded


def _case_rows(
    ap: argparse.ArgumentParser,
    cases: list[dict[str, Any]],
    fixed: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """잴 점검 세트 행. --responses면 그 파일에 있는 행만 잰다."""
    if fixed is None:
        return cases
    unknown = len(set(fixed) - {row["id"] for row in cases})
    rows = [row for row in cases if row["id"] in fixed]
    sys.stdout.write(f"응답 파일: 점검 세트 행 {len(rows)}개를 잰다")
    sys.stdout.write(f"(점검 세트에 없는 id {unknown}개는 뺀다)\n")
    if not rows:
        ap.error("응답 파일에 점검 세트 행 id가 없다")
    return rows


def _inputs(ap: argparse.ArgumentParser, args: argparse.Namespace) -> tuple[Any, ...]:
    """API를 부르기 전에 입력을 모두 읽고 검사한다.

    돌려주는 것: (행, 저장 응답, 판정기, 비교 점수, 튜터 설정, 덮은 설정, 임베딩 예상 호출,
    뺀 홀드아웃 행 수, 가드 설정, 공개 부분 세트). 행에는 홀드아웃 행이 없다. 가드 설정은 프리셋
    실행에만 있다. --cases 파일이 공개 튜터 행만 담았으면 공개 부분 세트(public_subset)로 잰다.
    """
    if args.runs < 1:
        ap.error("--runs는 1 이상이어야 한다")
    cfg, overrides = _tutor_config(ap, args)
    guard = None
    if args.preset is not None:  # --responses의 답은 다른 프로세스가 만들었다
        try:
            guard, changed = guard_config_used()
        except (OSError, ValueError, yaml.YAMLError) as exc:
            ap.error(f"가드 설정을 읽지 못했다: {type(exc).__name__}")
        if changed:  # ablation 설정·다른 KB 폴더면 튜터 검색 순위가 바뀐다. 놓치지 않게 알린다
            sys.stdout.write(f"가드 설정 기본 아님: {' · '.join(changed)}\n")
    if args.max_calls is None:
        args.max_calls = cfg.eval.max_calls
    judge = judge_from_env() if args.judge else None
    if args.judge and judge is None:
        ap.error("--judge에는 OPENAI_BASE_URL·OPENAI_MODEL·OPENAI_API_KEY가 모두 필요하다")
    subset = None
    if args.cases not in ("public", "team"):
        try:
            subset = public_subset(args.cases)
        except (OSError, ValueError) as exc:
            ap.error(str(exc))
    kind = "public" if args.cases == "public" or subset is not None else "team"
    if kind == "team" and args.preset not in (None, "tutor"):  # reference는 정답 답변을 낸다
        ap.error("--cases는 --preset tutor나 --responses와 쓴다(점검 세트에는 정답 답변이 없다)")
    try:
        fixed = load_responses(args.responses) if args.responses else None
        previous = (
            load_previous(args.compare, judge=args.judge, cases=kind) if args.compare else None
        )
        cases = case_set.load_for_run(args.cases) if kind == "team" else None
    except (OSError, ValueError) as exc:
        ap.error(str(exc))
    if cases is None:
        rows, excluded = _measured_rows(ap, fixed, subset)
    else:  # 점검 세트는 공개 세트 행이 아니라 홀드아웃이 없다
        rows, excluded = _case_rows(ap, cases, fixed), 0
    if args.guard and args.preset != "tutor":
        ap.error("--guard는 --preset tutor에서만 쓴다")
    if args.guard and cases is not None:  # 점검 세트 행은 공유 분할 밖이라 폴드별 가드가 없다
        ap.error("--guard는 공개 세트(--cases public)에서만 쓴다")
    if args.guard and not _guard_used(cfg):
        # 가드를 한 번도 부르지 않는데 clean으로 끝나 "가드 붙인 측정"처럼 --compare 기준이 되고,
        # 가드 몫만 튜터 상한에서 빠진다(#165 리뷰 danielcho02 2차)
        ap.error(
            "--guard인데 가드를 부르는 설정이 없다: "
            "--tutor-set으로 toggles.guard_classify(serial·parallel)나 "
            "toggles.verify=guard를 켠다(mode=baseline은 가드를 쓰지 않는다)"
        )
    calls = expected_calls(
        rows,
        preset=args.preset,
        runs=args.runs,
        judge=args.judge,
        tutor_calls=CALLS_PER_REQUEST[cfg.mode],
    )
    if args.guard:  # 가드 classify·judge_output은 TUTOR_CALLS 안에 이미 있다. probe만 더한다
        calls += guard_probe_calls(load_guard_config().llm)
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
    return rows, fixed, judge, previous, cfg, overrides, embedding, excluded, guard, subset


def main(argv: Sequence[str] | None = None) -> int:
    ap = _parser()
    args = ap.parse_args(argv)
    inputs = _inputs(ap, args)
    rows, fixed, judge, previous, cfg, overrides, embedding, excluded, guard, subset = inputs
    team = args.cases != "public" and subset is None
    reserve = expected_calls(rows, preset=None, runs=args.runs, judge=args.judge)  # 판정 몫
    tutor_cap = args.max_calls - reserve - embedding
    guard_cap, guard_probe = 0, 0
    if args.guard:  # 가드 몫(probe + 요청당 classify·judge_output)을 튜터 몫에서 떼어 준다
        guard_probe = guard_probe_calls(load_guard_config().llm)
        per_run = GUARD_CALLS_PER_REQUEST * len(rows) * args.runs
        guard_cap = guard_probe + (per_run * 11 + 9) // 10
        tutor_cap -= guard_cap
    stop = RateLimitStop()  # 튜터 LLM·판정이 같이 쓴다(같은 팀 키)
    state, counted, built = None, None, (0, 0)
    guard_llm = None
    if args.preset == "tutor":
        state, counted, built = _tutor_state(cfg, overrides, tutor_cap, guard=args.guard)
        stop.watch_llm(state.tutor_llm)
        gate.track(state.tutor_llm)  # 요청당 nano 호출 수(#83 관문)
    if counted is not None and state.tutor_emb is None:
        ap.error("임베딩을 켰지만 KB 임베딩을 만들지 못했다(이대로 재면 BM25 수치다)")
    if counted is not None:  # KB 임베딩(기동)은 이미 끝났다. 질의 임베딩만 막는다
        state.tutor_emb = stop.watch_embed(state.tutor_emb)
    latency: list[float] = []
    calls_log: list[int] = []  # 요청마다 보낸 nano 시도 수(튜터·가드)
    checks: list[dict[str, bool | None]] | None = None
    if fixed is None:  # 자기 state가 아니면 tutor.handle(기본 state)로 부른다
        own = state if _own_state(cfg, overrides, guard=args.guard) else None
        setup = None
        if args.guard:
            if own is None:
                ap.error("가드를 붙인 측정은 자기 state가 있어야 한다")
            guard_llm = LLM(load_guard_config().llm)
            # probe는 complete를 거치지 않아 calls에 세지 않는다. 그 몫을 빼야 합이 가드 몫이다
            guard_llm.max_calls = guard_cap - guard_probe
            guard_llm.run_errors = _count_errors(guard_llm)
            gate.track(guard_llm)
            stop.watch_llm(guard_llm)  # 가드 쪽 429도 세고, 어느 쪽에서 멈춰도 둘 다 막는다
            setup = _guard_setup(ap, own, cfg, rows, guard_llm)
        elif own is not None and cfg.toggles.fewshot != "off":
            # 평가 묶음(점검 세트는 홀드아웃 묶음)을 예시 색인에서 뺀다(TU-08·TU-16)
            own = (
                FoldStates(case_set.example_states(own, cfg, rows))
                if team
                else fold_states(own, cfg, rows)
            )
        if (
            setup is None
            and args.preset == "tutor"
            and cfg.toggles.fewshot != "off"
            and not isinstance(own, FoldStates)
        ):
            # 행별 색인이 아니면 평가 행 자신이 예시 풀에 든다. 조용히 부풀린 점수를 내지 않는다
            ap.error("판단 예시를 켠 측정은 행마다 평가 묶음을 뺀 예시 색인을 써야 한다(TU-16)")
        if args.preset == "tutor" and cfg.toggles.evidence_check:
            checks = []  # 실행마다 행 → 근거 대조 칸 값(#162)
        if setup is None:
            all_responses, latency = collect_timed(
                args.preset, rows, args.runs, own, checks, calls_log=calls_log
            )
        else:  # 가드 LLM·폴드별 가드는 응답을 모으는 루프에서 만든다
            all_responses, latency = collect_timed(
                args.preset, rows, args.runs, checks=checks, setup=setup, calls_log=calls_log
            )
    else:
        all_responses = [fixed] * args.runs
    llm_stopped = stop.tripped  # 튜터 단계에서 멈췄나(판정 단계에서 멈춘 것과 가른다)
    stop.next_stage()
    kb = score_local.load_kb_dir(KB_DIR)
    scorer = stop.watch_judge(judge) if judge is not None else None
    scored = [
        metrics.score_run(rows, responses, judge=scorer, kb=kb) for responses in all_responses
    ]
    report = metrics.build_report(scored)  # 홀드아웃 행은 이미 뺐다: 교차 검증 행 = 모든 행
    by_axis = case_set.axis_scores(scored, rows, judged=judge is not None) if team else None
    blocked = sum(state.tutor_llm.blocked.values()) if state is not None else 0
    failed = sum(state.tutor_llm.failures.values()) if state is not None else 0
    use = _embedding_use(counted, built, rows, args.runs)
    guard_run = _guard_run(guard_llm, getattr(state, "guard_skips", None))
    tainted = bool(blocked) or bool(failed) or (use is not None and not use.ok) or stop.tripped
    # 가드 단계를 건너뛰었거나 가드 호출이 오류로 끝난 행은 튜터 단독·가드 기본값 결과다(#165 리뷰)
    tainted = tainted or (guard_run is not None and not guard_run["clean"])
    invalid = stop.reason()
    name = (args.preset or args.responses.stem) + ("-team" if team else "")
    name += "-subset" if subset is not None else ""
    check_summary = (
        evidence_check_summary(rows, all_responses, checks) if checks is not None else None
    )
    gate_summary = (
        gate.summarize(
            rows,
            report,
            judged=judge is not None,
            latency=latency,
            per_request=calls_log,
            p50=_pct(latency, 0.5),
            p95=_pct(latency, 0.95),
        )
        if state is not None
        else None
    )
    path = _write(
        report,
        gate_summary=gate_summary,
        tutor=_tutor_config_used(cfg, overrides) if args.preset == "tutor" else None,
        name=name,
        judge=judge is not None,
        out_dir=args.out,
        tainted=tainted,
        embedding=use,
        invalid=invalid,
        holdout_excluded=excluded,
        guard=guard,
        guard_run=guard_run,
        llm=state.tutor_llm if state is not None else None,
        checks=checks,
        check_summary=check_summary,
        team=None if by_axis is None else {"file": args.cases, "axes": case_set.axes_json(by_axis)},
        subset=_subset_used(subset, rows),
    )
    _show(report, sys.stdout, judged=judge is not None)
    if check_summary is not None:
        show_evidence_check(check_summary, sys.stdout)
    if by_axis is not None:
        case_set.show_axes(by_axis, args.runs, sys.stdout)
    if state is not None:
        cap = (tutor_cap, args.max_calls, reserve, embedding)
        _show_tutor_calls(state, blocked, latency, cap=cap, stopped=llm_stopped)
    if gate_summary is not None:
        gate.show(gate_summary, sys.stdout)
    if guard_run is not None:
        _show_guard(guard_run)
    if use is not None:
        embedding_summary(use, sys.stdout)
    if previous is not None and invalid is None:  # 멈춘 실행은 기본값이 섞여 비교하지 않는다
        lost = metrics.regressions(previous, report)
        sys.stdout.write(f"새로 틀린 문항 {len(lost)}개{_ids(lost)}\n")
        skipped = len(previous.keys() - report.points.keys())
        if skipped:  # 홀드아웃 행이 섞인 이전 결과(200행) 등. 재지 않은 행은 틀린 것이 아니다
            sys.stdout.write(f"이전 결과에만 있는 행 {skipped}개는 비교하지 않는다\n")
    sys.stdout.write(f"저장: {path}\n")
    if invalid is not None:
        sys.stderr.write(f"{invalid}. 결과는 무효다\n")
        return EXIT_RATE_LIMITED
    return 1 if tainted else 0


def _guard_setup(
    ap: argparse.ArgumentParser,
    state: Any,
    cfg: TutorConfig,
    rows: Sequence[dict[str, Any]],
    llm: Any,
) -> Callable[[], Awaitable[FoldStates]]:
    """--guard: 측정 루프 안에서 가드 LLM을 기동(probe)하고 폴드별 가드로 행마다 state를 만든다.

    가드 LLM 클라이언트는 만든 루프에 묶이므로 응답을 모으는 루프에서 만든다(collect_timed setup).
    """

    async def setup() -> FoldStates:
        await llm.start()  # 인증 거절은 여기서 예외다
        base_error = (getattr(llm, "probe_result", None) or {}).get("base")
        if base_error:  # 막힌 키에 가드 호출을 보내지 않는다(가드 하네스 #151과 같다)
            ap.error(f"가드 LLM probe의 맨 요청이 {base_error}로 실패했다. 측정을 시작하지 않는다")
        states = fold_states(state, cfg, rows, guards=await build_fold_guards(rows, llm=llm))
        if not isinstance(states, FoldStates):
            # 폴드별 가드가 아니면 평가 행이 가드 kNN·LR에 든다(TU-10). 튜터를 부르기 전에 멈춘다
            ap.error("가드를 붙인 측정은 행마다 평가 묶음을 뺀 가드를 써야 한다(TU-10·TU-16)")
        return states

    return setup


def _count_errors(llm: Any) -> Counter[str]:
    """가드 LLM complete의 최종 오류 코드(budget·rate_limit 등)를 센다(본문 없음).

    가드 판정은 nano 호출이 실패해도 예외 없이 kNN·LR 기본값으로 답한다. 그런 행은 튜터의 가드
    건너뜀으로 잡히지 않으므로, 호출 결과 오류를 따로 세어 결과를 믿을 수 있는지 본다(#165 리뷰).
    """
    errors: Counter[str] = Counter()
    inner = llm.complete

    async def complete(*args: Any, **kwargs: Any) -> Any:
        result = await inner(*args, **kwargs)
        if result.error:
            errors[result.error] += 1
        return result

    llm.complete = complete
    return errors


def _guard_run(llm: Any, skips: Any) -> dict[str, Any] | None:
    """--guard 결과 JSON의 guard_run: 가드 호출 수(probe 제외)·상한·오류 코드별·건너뜀 사유별 수.

    clean은 가드 LLM 최종 오류가 0이고 `GUARD_BAD_SKIPS`(missing·error·invalid) 건너뜀이 0일 때
    참이다. 아니면 그 행은 가드가 서버와 다르게 답한 튜터 단독·가드 기본값 결과라 범주 판정·검증
    비교에 쓰지 않는다(tainted). timeout·no_time 건너뜀은 시간 제한이 설계대로 작동한 것이라
    skips에 수로만 남기고 clean을 바꾸지 않는다(#165 리뷰 danielcho02 2차).
    """
    if llm is None:
        return None
    errors = dict(sorted(getattr(llm, "run_errors", {}).items()))
    pairs = sorted((skips or {}).items())
    skipped = {f"{stage} {reason}": n for (stage, reason), n in pairs}
    bad = any(n for (_stage, reason), n in pairs if reason in GUARD_BAD_SKIPS)
    return {
        "calls": dict(sorted(llm.calls.items())),
        "max_calls": llm.max_calls,
        "errors": errors,
        "skips": skipped,
        "clean": not errors and not bad,
    }


def _show_guard(run: Mapping[str, Any]) -> None:
    """--guard: 가드 LLM 호출 수(probe 제외, 모든 실행 합), 오류, 튜터가 가드 단계를 건너뛴 횟수."""
    calls = ", ".join(f"{role} {n}회" for role, n in run["calls"].items()) or "0회"
    sys.stdout.write(f"가드 LLM 호출: {calls} (상한 {run['max_calls']}회)\n")
    errors = ", ".join(f"{code} {n}회" for code, n in run["errors"].items())
    sys.stdout.write(f"가드 LLM 최종 오류: {errors or '없음'}\n")
    shown = ", ".join(f"{name} {n}회" for name, n in run["skips"].items())
    sys.stdout.write(f"가드 단계 건너뜀(모든 실행 합): {shown or '없음'}\n")
    if not run["clean"]:
        sys.stdout.write(
            "가드가 돌지 않았거나 기본값으로 답한 호출이 있다: 비교 기준으로 쓰지 않는다\n"
        )
    elif run["skips"]:
        sys.stdout.write("시간 제한 건너뜀은 설계대로라 비교 기준에서 빼지 않는다(수만 남긴다)\n")


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
