"""부하 시험(S2.10, GR-10): 공개 세트의 가드 요청을 채점기처럼 동시 64로 다시 보낸다.

채점하지 않는다(시간 초과·형식·지연만 센다). 홀드아웃 묶음의 행은 보내지 않는다.
실행(guardrail/에서, 서버는 따로 띄운다. 토큰은 환경변수 TEAM_TOKEN):
`uv run python -X utf8 -m guardrail.eval.load --url http://127.0.0.1:8000/guard --out r.json`
`--access-log 파일`을 주면 서버 접근 로그의 오류 칸에서 기본값으로 떨어진 판정을 역할별로 센다.
접근 로그 없이는 모두 ok여도 모델 호출이 성공 여부를 알 수 없다(실패해도 서버는 200+기본값).
먼저 `--n 20`으로 짧게 돌려 접근 로그의 오류 칸을 확인한 뒤 전체를 돌린다.
`--kb-bench`는 서버 없이 KB 검색 시간만 잰다(API 0회).
"""

import argparse
import json
import os
import random
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from guardrail.config import load_config
from guardrail.data import PublicRow, load_public
from guardrail.eval.cv import load_splits
from guardrail.kb import KbIndex

SPEC_TIMEOUT_S = 30.0  # 규격의 요청당 제한(SPEC §3)
MAX_RESPONSE_BYTES = 256 * 1024  # 채점기가 읽는 응답 상한
# --big 요청의 글자 수(UTF-8 기준 약 1.06MB). 전송은 ensure_ascii 이스케이프로 약 2.1MB다
BIG_BYTES = 1024 * 1024
WARN_NO_LOG = (
    "경고: --access-log 없이는 모두 ok여도 모델 호출이 성공했는지 알 수 없다"
    "(실패해도 서버는 200과 기본값을 준다).\n"
    "먼저 --n 20으로 짧게 돌려 접근 로그의 오류 칸을 확인한 뒤 전체를 돌려라.\n"
)
Send = Callable[[dict], tuple[int, bytes]]


def guard_rows() -> list[PublicRow]:
    """홀드아웃 묶음을 뺀 가드 행."""
    split = load_splits()
    held = set(split.holdout)
    rows = load_public(load_config().paths.public)
    return [r for r in rows if r.endpoint == "guard" and split.cluster[r.id] not in held]


def build_payloads(rows: list[PublicRow], n: int, big: int = 0, seed: int = 13) -> list[dict]:
    """행을 돌려 n개. 앞의 big개는 이력을 부풀린 뒤 전체를 섞는다.

    크기: 글자 수로 UTF-8 약 1.06MB, 전송은 \\uXXXX 이스케이프로 약 2.1MB.
    """
    out = []
    for i in range(n):
        r = rows[i % len(rows)]
        messages = [{"role": m.role, "content": m.content} for m in r.messages]
        if i < big:
            pad = [{"role": "user", "content": "가" * 2000}, {"role": "assistant", "content": "나"}]
            messages = pad * (BIG_BYTES // 6000) + messages
        p = {"spec_version": "1.0", "session_id": f"load_{i:04d}", "messages": messages}
        if r.candidate_answer is not None:
            p["candidate_answer"] = r.candidate_answer
        out.append(p)
    random.Random(seed).shuffle(out)  # noqa: S311 — 순서 섞기일 뿐이다
    return out


def classify(payload: dict, status: int, body: bytes) -> str:
    """ok | http | json | format. 형식은 계약(guard.schema.json)의 채점 필드만 본다."""
    if status != 200:
        return "http"
    try:
        d = json.loads(body)
        inp = d["input"]
        ok = inp["decision"] in ("allow", "block") and isinstance(inp["category"], str)
        if "candidate_answer" in payload:
            o = d["output"]
            ok = ok and isinstance(o["grounded"], bool) and isinstance(o["safe"], bool)
            ok = ok and all(isinstance(s, str) for s in o["unsupported"])
    except ValueError:
        return "json"
    except (KeyError, TypeError):
        return "format"
    return "ok" if ok else "format"


def _pct(values: list[float], q: float) -> float:
    s = sorted(values)
    return round(s[min(len(s) - 1, int(q * len(s)))], 3) if s else 0.0


def _latency(values: list[float]) -> dict:
    return {
        "n": len(values),
        "p50": round(statistics.median(values), 3) if values else 0.0,
        "p95": _pct(values, 0.95),
        "max": round(max(values, default=0.0), 3),
    }


def run_load(
    send: Send, payloads: list[dict], concurrency: int = 64, timeout_s: float = SPEC_TIMEOUT_S
) -> dict:
    """모두 보내고 집계한다. timeout_s를 넘겨 온 응답도 시간 초과로 센다(채점기는 0점)."""

    def one(p: dict) -> tuple[str, float, bool]:
        t0 = time.perf_counter()
        try:
            status, body = send(p)
            kind = classify(p, status, body)
        except TimeoutError:
            kind = "timeout"
        except Exception as exc:  # 연결 실패 등. 종류만 센다
            kind = "timeout" if "timed out" in str(exc) else "error"
        dt = time.perf_counter() - t0
        return ("timeout" if dt > timeout_s else kind), dt, len(p["messages"]) > 100

    t0 = time.perf_counter()
    with ThreadPoolExecutor(concurrency) as ex:
        results = list(ex.map(one, payloads))
    counts = Counter(k for k, _, _ in results)
    return {
        "requests": len(payloads),
        "with_candidate": sum("candidate_answer" in p for p in payloads),
        "concurrency": concurrency,
        "wall_s": round(time.perf_counter() - t0, 1),
        "counts": {
            k: counts.get(k, 0) for k in ("ok", "timeout", "http", "json", "format", "error")
        },
        "latency_s": _latency([dt for _, dt, big in results if not big]),
        "latency_big_s": _latency([dt for _, dt, big in results if big]),
    }


def http_sender(url: str, token: str, timeout_s: float = SPEC_TIMEOUT_S) -> Send:
    def send(payload: dict) -> tuple[int, bytes]:
        req = urllib.request.Request(  # noqa: S310 — 주소는 실행하는 사람이 준다
            url,
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as r:  # noqa: S310
                return r.status, r.read(MAX_RESPONSE_BYTES)
        except urllib.error.HTTPError as exc:
            return exc.code, b""

    return send


def log_failures(lines: list[str], requests: int, with_candidate: int) -> dict:
    """접근 로그의 /guard 줄에서 오류 칸(`역할[:예외]`를 쉼표로 이음)을 역할별로 센다."""
    roles: Counter[str] = Counter()
    seen = 0
    for line in lines:
        f = line.split(" ")
        if len(f) < 7 or f[-6] != "/guard" or f[-3] != "200":  # 401 같은 거절은 판정이 아니다
            continue
        seen += 1
        if f[-1] != "-":
            roles.update({code.split(":")[0] for code in f[-1].split(",")})
    denom = {"input_judge": requests, "output_judge": with_candidate, "output": with_candidate}
    return {
        "guard_lines": seen,
        "by_role": dict(roles),
        "rate": {r: round(n / (denom.get(r) or requests), 4) for r, n in roles.items()},
    }


def kb_bench() -> dict:
    """KbIndex.rank 한 번의 시간(가드 후보 답변 행 + 튜터 행의 마지막 학습자 발화)."""
    cfg = load_config()
    kb = KbIndex.load(cfg.paths.kb, ngram=cfg.kb.ngram, k1=cfg.kb.k1, b=cfg.kb.b)
    rows = load_public(cfg.paths.public)
    queries = [
        next(m.content for m in reversed(r.messages) if m.role == "user")
        for r in rows
        if r.endpoint == "tutor" or r.candidate_answer
    ]
    times = []
    for q in queries:
        t0 = time.perf_counter()
        kb.rank(q)
        times.append((time.perf_counter() - t0) * 1000)
    return {"queries": len(queries), "p50_ms": _pct(times, 0.5), "p95_ms": _pct(times, 0.95)}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000/guard")
    ap.add_argument("--n", type=int, default=600)
    ap.add_argument("--concurrency", type=int, default=64)
    ap.add_argument(
        "--big",
        type=int,
        default=0,
        help="이력을 부풀릴 요청 수(글자 수로 약 1MB, \\uXXXX 이스케이프로 전송은 약 2.1MB)",
    )
    ap.add_argument("--access-log", help="서버 접근 로그 파일(오류 칸 집계)")
    ap.add_argument("--kb-bench", action="store_true")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    if args.kb_bench:
        report = {"kb": kb_bench()}
    else:
        if not args.access_log:
            sys.stderr.write(WARN_NO_LOG)
        payloads = build_payloads(guard_rows(), args.n, args.big)
        report = run_load(
            http_sender(args.url, os.environ["TEAM_TOKEN"]), payloads, args.concurrency
        )
        if args.access_log:
            lines = Path(args.access_log).read_text("utf-8", errors="replace").splitlines()
            report["failures"] = log_failures(lines, report["requests"], report["with_candidate"])
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", "utf-8")
    sys.stdout.write(text + "\n")


if __name__ == "__main__":
    main()
