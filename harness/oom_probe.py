"""걸러 보기 대조 호출 측정(이슈 #162, I2.7). 저장소 루트에서 `python -m harness.oom_probe`.

주 호출과 같은 입력(근거 묶음 · 대화 창)으로 "묻는 것이 근거에 있는가"만 묻는 대조 호출을 공개 튜터
세트의 교차 검증 160행(홀드아웃 제외)에 1회씩 보내고 행마다 기록한다. 서버 코드는 바꾸지 않는다.

- 행 · 홀드아웃: `tutor.eval.run_local`의 `public_tutor_rows` · `holdout_ids`. 160행이 아니면
  멈춘다.
- 근거 묶음 · 대화: 서버와 같이 요청 해석(`parse_request`) → 대화 정리(`tidy`) → 검색
  (`retrieve`, 튜터 설정의 `retrieval.k`, 기동 때와 같은 KB 색인) → 주 호출의 블록 함수
  (`_section_block` · `_turn_block`, 비공개). user 메시지는 근거 묶음과 대화 두 블록만이다.
- system: harness/prompts/oom_check.md(첫 줄 출처 주석은 보내지 않는다).
- 호출: 튜터 LLM 호출기(`tutor.runtime.llm`, 주 호출과 같은 모델 · 키 · 파라미터 · 게이트),
  json_schema strict, `--effort`로 reasoning_effort를 덮는다. 재시도 없음. 동시 요청은
  `tutor.eval.metrics.WORKERS`개. 429가 연달아 3번이면 뒤 행은 보내지 않는다.
- 기록: harness/runs/oom-<effort>-<시각>.json(git 제외). asked · quote 원문은 `--keep-text`일 때만.
- 요약(표준 출력, 본문 없음): 호출 · 실패 갈래 · 잘림 비율, 지연, 정답 action 묶음별 found 혼동,
  `--main`(run_local 결과 JSON)을 주면 "주 호출 answer를 abstain으로 바꾸면" 모의와 채택 기준 판정.

run_local 결과 JSON에는 주 호출 지연이 없다(화면에만 낸다). 주 호출 p50 · p95는 `--main-p50` ·
`--main-p95`로 받는다. 키 · 토큰 값은 출력하지 않는다.
"""

import argparse
import asyncio
import contextvars
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TextIO

from pydantic import BaseModel, ConfigDict

from tutor.eval import metrics
from tutor.eval.run_local import EXIT_RATE_LIMITED, _pct, holdout_ids, public_tutor_rows
from tutor.judge.conversation import tidy
from tutor.judge.flow import _messages
from tutor.judge.main_call import _section_block, _turn_block
from tutor.request import parse_request
from tutor.retrieval import retrieve
from tutor.runtime.config import load_config
from tutor.runtime.llm import Result, TutorLLM, missing_api_env
from tutor.startup import prepare

ROOT = Path(__file__).resolve().parent
PROMPT_PATH = ROOT / "prompts" / "oom_check.md"
RUNS_DIR = ROOT / "runs"
ROLE = "oom_check"  # TutorLLM.calls에 세는 역할 이름
CV_ROWS = 160  # 공개 튜터 200행 - 홀드아웃 40행
TARGET_MARK = '판단대상="예"'
EMPTY_BUNDLE = "(관련 자료를 찾지 못했습니다)"  # main_call.build_user_message와 같은 문구
RATE_LIMIT_STOP = 3  # 429가 연달아 이만큼이면 뒤 행은 보내지 않는다

# 실패 갈래(행 기록 · 요약에 쓰는 이름)
TIMEOUT, LENGTH, BAD_JSON = "시간 초과", "length 잘림", "JSON 깨짐"
RATE_LIMIT, OTHER = "429", "그 밖"
FAILURES = (TIMEOUT, LENGTH, BAD_JSON, RATE_LIMIT, OTHER)
NOT_SENT = "멈춤으로 안 보냄"  # 연속 429로 멈춘 뒤의 행(호출 수에 들지 않는다)
QUOTE_OK, QUOTE_UNVERIFIED = "ok", "확인 안 됨"

# 채택 기준(이슈 #162)
MAX_TRUNCATION_PCT = 5.0
MIN_NET_GAIN = 5
MAX_WRONG = 3

GROUPS = ("abstain", "answer·scaffold", "그 밖")


class OomCheckOut(BaseModel):
    """대조 호출 출력(json_schema strict, 필드 순서 = 생성 순서)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    asked: str
    quote: str
    found: bool


def load_system(path: Path = PROMPT_PATH) -> str:
    """system 문구. 첫 줄 출처 주석(<!-- … -->)은 빼고 나머지를 글자 그대로(앞뒤 빈 줄만 뺀다)."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    first, _, rest = text.partition("\n")
    if not (first.startswith("<!--") and first.endswith("-->")):
        raise ValueError(f"{path.name} 첫 줄에 출처 주석이 없다")
    return rest.strip("\n")


@dataclass(frozen=True)
class Prepared:
    """행 하나의 대조 호출 입력. 메시지는 메모리에만 있고 기록하지 않는다."""

    id: str
    gold_category: str
    gold_action: str
    messages: list[dict[str, str]]
    bundle: str  # 근거 묶음 안쪽(보낸 그대로). quote 확인에 쓴다


def build_user(bundle: str, turns: str) -> str:
    """근거 묶음과 대화 두 블록만(판단 예시 · 신호 · 샌드위치 규칙은 넣지 않는다)."""
    return f"<근거 묶음>\n{bundle}\n</근거 묶음>\n\n{turns}"


async def prepare_row(
    row: Mapping[str, Any], system: str, *, k: int, index: Any = None
) -> Prepared:
    """서버 주 호출과 같은 근거 묶음 · 대화 창을 만든다.

    대화 블록의 판단 대상 표시가 한 번이 아니면 ValueError(API를 부르기 전에 멈춘다).
    """
    req = parse_request({"messages": row["messages"]})
    if req is None:
        raise ValueError(f"{row['id']}: 요청을 읽지 못했다")
    conv = tidy(_messages(req))
    query, history = conv.search_query()
    extra = {"index": index} if index is not None else {}
    sections = await retrieve(query, history, k=k, **extra)
    bundle = "\n".join(_section_block(s) for s in sections) or EMPTY_BUNDLE
    turns = _turn_block(conv)
    if turns.count(TARGET_MARK) != 1:
        raise ValueError(f"{row['id']}: 대화 블록의 {TARGET_MARK}가 {turns.count(TARGET_MARK)}번")
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": build_user(bundle, turns)},
    ]
    labels = row["labels"]
    return Prepared(row["id"], labels["category"], labels["action"], messages, bundle)


def squash(text: str) -> str:
    """연속 공백 · 줄바꿈을 한 칸으로, 앞뒤 공백은 뺀다."""
    return " ".join(text.split())


def quote_check(found: bool, quote: str, bundle: str) -> str | None:
    """found=true일 때만 본다: 정규화한 quote가 정규화한 근거 묶음의 부분 문자열이면 ok."""
    if not found:
        return None
    q = squash(quote)
    return QUOTE_OK if q and q in squash(bundle) else QUOTE_UNVERIFIED


def failure_of(result: Result) -> str | None:
    """실패 갈래. finish_reason이 length면 파싱이 됐어도 잘림으로 센다(found로 치지 않는다)."""
    if result.error == "timeout":
        return TIMEOUT
    if result.finish_reason == "length":
        return LENGTH
    if result.error == "bad_json":
        return BAD_JSON
    if result.error == "rate_limit":
        return RATE_LIMIT
    if result.error is not None or not isinstance(result.parsed, OomCheckOut):
        return OTHER
    return None


# 이 행(요청) 하나의 응답 usage에서 읽은 추론 토큰. 행마다 새 상자를 넣는다
_USAGE: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "oom_usage", default=None
)


def capture_usage(send):
    """전송을 감싸 응답의 reasoning_tokens(있으면)만 행의 상자에 적는다. 본문은 보지 않는다."""

    async def wrapped(kwargs: dict[str, Any]) -> Any:
        resp = await send(kwargs)
        box = _USAGE.get()
        if box is not None:
            details = getattr(getattr(resp, "usage", None), "completion_tokens_details", None)
            tokens = getattr(details, "reasoning_tokens", None)
            box["reasoning_tokens"] = tokens if isinstance(tokens, int) else None
        return resp

    return wrapped


def record(item: Prepared, result: Result, usage: Mapping[str, Any], keep_text: bool) -> dict:
    failure = failure_of(result)
    out = result.parsed if failure is None else None
    row: dict[str, Any] = {
        "id": item.id,
        "gold_category": item.gold_category,
        "gold_action": item.gold_action,
        "found": None if out is None else out.found,
        "quote_check": None if out is None else quote_check(out.found, out.quote, item.bundle),
        "asked_chars": None if out is None else len(out.asked),
        "quote_chars": None if out is None else len(out.quote),
        "latency_ms": round(result.latency_ms, 1),
        "finish_reason": result.finish_reason,
        "completion_tokens": result.completion_tokens,
        "reasoning_tokens": usage.get("reasoning_tokens"),
        "failure": failure,
        "error": result.error,  # 오류 코드만(메시지 · 본문 없음)
    }
    if keep_text and out is not None:
        row["asked"], row["quote"] = out.asked, out.quote
    return row


async def probe(
    items: Sequence[Prepared],
    llm: TutorLLM,
    *,
    timeout_s: float,
    keep_text: bool = False,
    workers: int = metrics.WORKERS,
) -> tuple[list[dict[str, Any]], bool]:
    """행마다 대조 호출 1회(재시도 없음). (행 기록, 연속 429로 멈췄는가)."""
    gate = asyncio.Semaphore(workers)
    streak = {"429": 0, "stopped": False}

    async def one(item: Prepared) -> dict[str, Any]:
        async with gate:
            if streak["stopped"]:
                return {"id": item.id, "gold_category": item.gold_category,
                        "gold_action": item.gold_action, "failure": NOT_SENT}  # fmt: skip
            box: dict[str, Any] = {}
            token = _USAGE.set(box)
            try:
                loop = asyncio.get_running_loop()
                result = await llm.complete(
                    ROLE,
                    item.messages,
                    OomCheckOut,
                    deadline=loop.time() + timeout_s,
                    timeout_s=timeout_s,
                    retries=0,
                )
            finally:
                _USAGE.reset(token)
            row = record(item, result, box, keep_text)
            streak["429"] = streak["429"] + 1 if row["failure"] == RATE_LIMIT else 0
            if streak["429"] >= RATE_LIMIT_STOP:
                streak["stopped"] = True
            return row

    rows = await asyncio.gather(*(one(item) for item in items))
    return list(rows), bool(streak["stopped"])


# --- 요약 ---


def group_of(action: str) -> str:
    if action == "abstain":
        return "abstain"
    return "answer·scaffold" if action in ("answer", "scaffold") else "그 밖"


def sent(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [r for r in rows if r.get("failure") != NOT_SENT]


def outcome(row: Mapping[str, Any]) -> str:
    """found 혼동 칸: found=true(ok) · 확인 안 됨 · found=false · 실패."""
    if row.get("failure") is not None:
        return "실패"
    if not row["found"]:
        return "found=false"
    return "found=true(ok)" if row["quote_check"] == QUOTE_OK else QUOTE_UNVERIFIED


OUTCOMES = ("found=true(ok)", QUOTE_UNVERIFIED, "found=false", "실패")


def calls_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    done = sent(rows)
    fails = Counter(r["failure"] for r in done if r["failure"] is not None)
    length = sum(r.get("finish_reason") == "length" for r in done)
    seconds = [r["latency_ms"] / 1000 for r in done]
    return {
        "calls": len(done),
        "ok": len(done) - sum(fails.values()),
        "failures": {name: fails.get(name, 0) for name in FAILURES},
        "not_sent": len(rows) - len(done),
        "truncation_pct": 100 * length / len(done) if done else 0.0,
        "p50": _pct(seconds, 0.5),
        "p95": _pct(seconds, 0.95),
        "max": max(seconds, default=0.0),
    }


def confusion(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    table = {g: dict.fromkeys(OUTCOMES, 0) for g in GROUPS}
    for r in sent(rows):
        table[group_of(r["gold_action"])][outcome(r)] += 1
    return table


class MainJsonError(ValueError):
    """run_local 결과 JSON이 예상한 모양이 아니다(추측하지 않고 멈춘다)."""


def load_main_actions(path: Path, run: int = 0) -> dict[str, str | None]:
    """run_local 결과 JSON의 `report.cv_emitted_by_run[run]`에서 행 id → 주 호출이 낸 action."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        by_run = data["report"]["cv_emitted_by_run"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        reason = type(exc).__name__
        raise MainJsonError(f"report.cv_emitted_by_run을 읽지 못했다({reason})") from exc
    if not isinstance(by_run, list) or not by_run:
        raise MainJsonError("report.cv_emitted_by_run이 비었거나 목록이 아니다")
    if not 0 <= run < len(by_run):
        raise MainJsonError(f"--main-run {run}: 실행은 0~{len(by_run) - 1}이다")
    emitted = by_run[run]
    if not isinstance(emitted, dict) or not emitted:
        raise MainJsonError(f"실행 {run}의 행 기록이 비었거나 사전이 아니다")
    actions: dict[str, str | None] = {}
    for row_id, info in emitted.items():
        if not isinstance(info, dict) or "action" not in info:
            raise MainJsonError(f"행 {row_id}에 action이 없다")
        actions[row_id] = info["action"]
    return actions


def simulate(
    rows: Sequence[Mapping[str, Any]], main_actions: Mapping[str, str | None], *, strict: bool
) -> dict[str, int]:
    """주 호출이 answer를 낸 행 중 대조 호출이 "없음"이면 abstain으로 바꿨을 때의 수.

    strict=False (가): found=false만 "없음". strict=True (나): found=false 또는 quote 확인 안 됨.
    실패한(또는 보내지 않은) 대조 호출은 "없음"이 아니다(주 호출 유지).
    """
    by_id = {r["id"]: r for r in rows}
    counts = {"targets": 0, "no_probe": 0, "right": 0, "wrong": 0, "other": 0}
    for row_id, action in main_actions.items():
        if action != "answer":
            continue
        counts["targets"] += 1
        r = by_id.get(row_id)
        if r is None:
            counts["no_probe"] += 1
            continue
        if r.get("failure") is not None:
            continue
        absent = r["found"] is False or (strict and r["quote_check"] == QUOTE_UNVERIFIED)
        if not absent:
            continue
        gold = r["gold_action"]
        if gold == "abstain":
            counts["right"] += 1
        elif gold in ("answer", "scaffold"):
            counts["wrong"] += 1
        else:
            counts["other"] += 1
    counts["net"] = counts["right"] - counts["wrong"]
    return counts


def adoption(
    calls: Mapping[str, Any], sim: Mapping[str, int], main_p95: float | None
) -> dict[str, bool | None]:
    """채택 기준. None은 판정할 값이 없음(주 호출 p95를 받지 못함)."""
    return {
        f"잘림 {MAX_TRUNCATION_PCT:g}% 미만": calls["truncation_pct"] < MAX_TRUNCATION_PCT,
        "대조 p95 < 주 호출 p95": None if main_p95 is None else calls["p95"] < main_p95,
        f"순이득 +{MIN_NET_GAIN} 이상": sim["net"] >= MIN_NET_GAIN,
        f"틀리게 {MAX_WRONG} 이하": sim["wrong"] <= MAX_WRONG,
    }


def _mark(value: bool | None) -> str:
    return "판정 불가" if value is None else "통과" if value else "미달"


def show(
    rows: Sequence[Mapping[str, Any]],
    out: TextIO,
    *,
    main_actions: Mapping[str, str | None] | None = None,
    main_p50: float | None = None,
    main_p95: float | None = None,
) -> None:
    c = calls_summary(rows)
    fails = " · ".join(f"{k} {v}" for k, v in c["failures"].items())
    out.write(f"1. 호출 {c['calls']} · 성공 {c['ok']} · 실패 갈래: {fails}")
    out.write(f" · 잘림 비율 {c['truncation_pct']:.1f}%")
    out.write(f" · 보내지 않음 {c['not_sent']}\n" if c["not_sent"] else "\n")
    out.write(
        f"2. 대조 호출 지연(초, 동시 {metrics.WORKERS}): p50 {c['p50']:.2f} · "
        f"p95 {c['p95']:.2f} · 최대 {c['max']:.2f}\n"
    )
    out.write("3. found 혼동(정답 action 묶음 → " + " · ".join(OUTCOMES) + ")\n")
    for group, cells in confusion(rows).items():
        out.write(f"   {group}: " + " · ".join(str(cells[k]) for k in OUTCOMES) + "\n")
    if main_actions is None:
        return
    p50 = "-" if main_p50 is None else f"{main_p50:.2f}"
    p95 = "-" if main_p95 is None else f"{main_p95:.2f}"
    out.write(f"4. 모의(주 호출 answer → abstain). 주 호출 지연 p50 {p50} · p95 {p95}")
    out.write(f" / 대조 p50 {c['p50']:.2f} · p95 {c['p95']:.2f}\n")
    verdicts = []
    for label, strict in (("(가) found=false", False), ("(나) found=false 또는 확인 안 됨", True)):
        s = simulate(rows, main_actions, strict=strict)
        out.write(
            f"   {label}: 대상 {s['targets']}(대조 기록 없음 {s['no_probe']}) · "
            f"맞게 {s['right']} · 틀리게 {s['wrong']} · 그 밖 {s['other']} · 순이득 {s['net']:+d}\n"
        )
        verdicts.append((label[:3], adoption(c, s, main_p95)))
    for label, verdict in verdicts:
        out.write(
            f"5. 채택 기준 {label}: "
            + " · ".join(f"{k} {_mark(v)}" for k, v in verdict.items())
            + "\n"
        )


# --- 실행 ---


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m harness.oom_probe",
        description="걸러 보기 대조 호출 측정(이슈 #162): 교차 검증 160행에 1회씩",
    )
    ap.add_argument("--effort", required=True, choices=("low", "medium"), help="reasoning_effort")
    ap.add_argument("--timeout-s", type=float, default=30.0, help="호출당 시간 제한(기본 30)")
    ap.add_argument("--keep-text", action="store_true", help="asked · quote 원문도 결과에(로컬용)")
    ap.add_argument("--main", type=Path, help="run_local 결과 JSON. 주면 모의 · 채택 기준을 낸다")
    ap.add_argument("--main-run", type=int, default=0, help="--main의 실행 번호(기본 0)")
    ap.add_argument("--main-p50", type=float, help="주 호출 지연 p50(초, run_local 화면 값)")
    ap.add_argument("--main-p95", type=float, help="주 호출 지연 p95(초, run_local 화면 값)")
    ap.add_argument("--out", type=Path, default=RUNS_DIR, help="결과 폴더(기본 harness/runs)")
    return ap


def cv_rows() -> list[dict[str, Any]]:
    public = public_tutor_rows()
    held = holdout_ids(public)
    return [row for row in public if row["id"] not in held]


def write_result(
    rows: Sequence[Mapping[str, Any]],
    *,
    out_dir: Path,
    effort: str,
    timeout_s: float,
    params: Mapping[str, Any],
    stopped: bool,
    keep_text: bool,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = out_dir / f"oom-{effort}-{stamp}.json"
    data = {
        "created": stamp,
        "effort": effort,
        "timeout_s": timeout_s,
        "workers": metrics.WORKERS,
        "params": dict(params),  # 보낸 파라미터 값(키 · 주소 없음)
        "keep_text": keep_text,
        "stopped": "rate_limit" if stopped else None,
        "summary": calls_summary(rows),
        "rows": list(rows),
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def main(argv: Sequence[str] | None = None) -> int:
    ap = _parser()
    args = ap.parse_args(argv)
    if args.timeout_s <= 0:
        ap.error("--timeout-s는 0보다 커야 한다")
    main_actions = None
    if args.main is not None:  # API를 부르기 전에 모양을 본다
        try:
            main_actions = load_main_actions(args.main, args.main_run)
        except MainJsonError as exc:
            ap.error(f"--main: {exc}")
    missing = missing_api_env()
    if missing:
        ap.error(f"운영진 API 환경변수가 필요하다: {'·'.join(missing)}")
    rows = cv_rows()
    if len(rows) != CV_ROWS:
        ap.error(f"교차 검증 행이 {len(rows)}개다({CV_ROWS}개여야 한다)")
    cfg = load_config(overrides={"llm.reasoning_effort": args.effort})
    if cfg.toggles.embedding:
        ap.error("튜터 설정의 임베딩 검색이 켜져 있다: 이 스크립트는 BM25 검색만 재현한다")
    state = SimpleNamespace()
    prepare(state, cfg)  # 서버 기동과 같은 KB 색인 · LLM 호출기
    llm: TutorLLM = state.tutor_llm
    if llm.structured != "json_schema":
        ap.error(f"구조화 방식이 {llm.structured}다(json_schema여야 한다)")
    llm._send = capture_usage(llm._send)  # 비공개 속성: 추론 토큰만 읽는 감싸개
    llm.max_calls = len(rows)  # 행마다 1회를 넘지 않는다
    system = load_system()

    async def run() -> tuple[list[dict[str, Any]], bool]:
        items = [
            await prepare_row(row, system, k=cfg.retrieval.k, index=state.tutor_kb) for row in rows
        ]
        sys.stdout.write(f"대조 호출 {len(items)}회(재시도 없음, 동시 {metrics.WORKERS})\n")
        return await probe(items, llm, timeout_s=args.timeout_s, keep_text=args.keep_text)

    records, stopped = asyncio.run(run())
    path = write_result(
        records,
        out_dir=args.out,
        effort=args.effort,
        timeout_s=args.timeout_s,
        params=llm.params,
        stopped=stopped,
        keep_text=args.keep_text,
    )
    show(
        records,
        sys.stdout,
        main_actions=main_actions,
        main_p50=args.main_p50,
        main_p95=args.main_p95,
    )
    sys.stdout.write(f"결과: {path.as_posix()}\n")
    if stopped:
        sys.stdout.write(f"429가 연달아 {RATE_LIMIT_STOP}번이라 뒤 행을 보내지 않았다\n")
        return EXIT_RATE_LIMITED
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
