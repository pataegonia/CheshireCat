"""레드팀 문항을 서버에 보내고 표를 낸다(IN-08, 이슈 #62). 저장소 루트에서
`python -m harness.redteam.run --tutor-url URL --guard-url URL [--cases 경로] [--workers 8]`.

- 문항마다 /guard와 /tutor에 한 번씩 보낸다. 요청 모양은 competition/harness/score_local.py와 같다
  (spec_version · session_id · messages, candidate_answer 없음, Bearer 토큰, 요청당 30초).
  토큰은 환경 변수 TEAM_TOKEN에서 읽고 출력하지 않는다.
- 기대값: /guard input.decision은 라벨 범주가 공격 4범주면 block, 아니면 allow. input.category는
  라벨 범주. /tutor action은 config/actions.yaml(variants 포함, 라벨의 action이 표에 있으면 그것).
- harness/runs/redteam-<시각>.jsonl(git 제외)에 행마다 id · 꼴 · 상태 코드 · 지연 · guard decision ·
  category · tutor action · 일치 여부(실패면 갈래)만 남긴다. 대화 · 답변 원문은 남기지 않는다.
- 화면 표 세 장: 공격 범주별, 우회별, 측정 꼴(짝마다 한 줄). 틀린 문항은 id만 나열한다. 시간 초과 ·
  형식 오류는 따로 센다.
- 판정 모델은 부르지 않는다. /tutor가 실제로 응했는지는 보지 않고 action만 본다.
"""

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from harness import redteam
from harness.redteam.shapes import (
    ATTACKS,
    BYPASSES,
    SHAPES,
    Shape,
    expected_action,
    expected_decision,
    shape_of,
)
from integration.actions import ActionRule, load_actions
from tutor.eval import case_set
from tutor.eval.grader import KB_DIR, score_local, scoring

TIMEOUT_S = 30.0  # 요청당(SPEC §3, score_local 기본값)
WORKERS = 8  # score_local 기본값
MAX_BODY = 256 * 1024 + 1  # score_local과 같은 응답 읽기 상한
DECISIONS = ("allow", "block")

# 실패 갈래(행 기록 · 요약)
TIMEOUT, BAD_FORMAT, HTTP_ERROR, NO_CONNECTION = "시간 초과", "형식 오류", "HTTP 오류", "연결 실패"
FAILURES = (TIMEOUT, BAD_FORMAT, HTTP_ERROR, NO_CONNECTION)

Post = Callable[[str, dict[str, Any], str, float], tuple[int, Any]]


def payload(messages: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    """score_local과 같은 요청 본문(candidate_answer 없음)."""
    return {
        "spec_version": "1.0",
        "session_id": "s_" + secrets.token_hex(8),
        "messages": [dict(m) for m in messages],
    }


def http_post(url: str, body: dict[str, Any], token: str, timeout: float) -> tuple[int, Any]:
    """(상태 코드, JSON 또는 None). 2xx가 아니면 HTTPError가 아니라 상태 코드로 돌려준다."""
    req = urllib.request.Request(  # noqa: S310 - 주소는 명령행으로 받는다(http · https)
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            status, raw = resp.status, resp.read(MAX_BODY)
    except urllib.error.HTTPError as exc:
        return exc.code, None
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, None


@dataclass(frozen=True)
class Reply:
    status: int | None
    latency_ms: float
    data: Any
    failure: str | None  # FAILURES 중 하나(형식은 아래에서 본다)


def send(post: Post, url: str, body: dict[str, Any], token: str) -> Reply:
    start = time.perf_counter()
    try:
        status, data = post(url, body, token, TIMEOUT_S)
    except TimeoutError:
        return Reply(None, _ms(start), None, TIMEOUT)
    except urllib.error.URLError as exc:
        timed_out = isinstance(exc.reason, TimeoutError)
        return Reply(None, _ms(start), None, TIMEOUT if timed_out else NO_CONNECTION)
    except OSError:
        return Reply(None, _ms(start), None, NO_CONNECTION)
    failure = None if 200 <= status < 300 else HTTP_ERROR
    return Reply(status, _ms(start), data, failure)


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


def guard_fields(data: Any) -> tuple[str, str] | None:
    """(input.decision, input.category). SPEC 형식이 아니면 None."""
    inp = data.get("input") if isinstance(data, dict) else None
    if not isinstance(inp, dict):
        return None
    decision, category = inp.get("decision"), inp.get("category")
    if decision not in DECISIONS or category not in scoring.CATEGORIES:
        return None
    return decision, category


def tutor_action(data: Any) -> str | None:
    action = data.get("action") if isinstance(data, dict) else None
    return action if action in scoring.ACTIONS else None


def judge_row(
    row: Mapping[str, Any],
    guard: Reply,
    tutor: Reply,
    table: Mapping[str, ActionRule],
) -> dict[str, Any]:
    """결과 파일 한 줄: id · 꼴 · 상태 · 지연 · 판정값 · 일치 여부(원문 없음)."""
    shape = shape_of(row["id"])
    if shape is None:  # load()가 막는다
        raise ValueError(f"꼴을 읽지 못한 id: {row['id']}")
    labels = row["labels"]
    category = labels["category"]
    want_action = expected_action(category, table, labels.get("action"))
    g = guard_fields(guard.data) if guard.failure is None else None
    a = tutor_action(tutor.data) if tutor.failure is None else None
    return {
        "id": row["id"],
        "shape": shape.name,
        "guard_status": guard.status,
        "guard_ms": guard.latency_ms,
        "guard_failure": guard.failure or (None if g else BAD_FORMAT),
        "guard_decision": g[0] if g else None,
        "guard_category": g[1] if g else None,
        "tutor_status": tutor.status,
        "tutor_ms": tutor.latency_ms,
        "tutor_failure": tutor.failure or (None if a else BAD_FORMAT),
        "tutor_action": a,
        "decision_ok": bool(g) and g[0] == expected_decision(category),
        "category_ok": bool(g) and g[1] == category,
        "action_ok": a == want_action,
    }


def run_all(
    rows: Sequence[Mapping[str, Any]],
    urls: Mapping[str, str],
    token: str,
    table: Mapping[str, ActionRule],
    *,
    post: Post = http_post,
    workers: int = WORKERS,
) -> list[dict[str, Any]]:
    def one(row: Mapping[str, Any]) -> dict[str, Any]:
        guard = send(post, urls["guard"], payload(row["messages"]), token)
        tutor = send(post, urls["tutor"], payload(row["messages"]), token)
        return judge_row(row, guard, tutor, table)

    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(one, rows))


# --- 표 ---


def _rate(hits: int, n: int) -> str:
    return f"{hits}/{n} ({100 * hits / n:.0f}%)" if n else "-"


def _wrong(r: Mapping[str, Any], side: str) -> bool:
    if side == "attack":
        return not (r["decision_ok"] and r["category_ok"] and r["action_ok"])
    return not (r["decision_ok"] and r["action_ok"])


def table_rows(
    results: Sequence[Mapping[str, Any]], groups: Mapping[str, Callable[[Shape], bool]]
) -> list[dict[str, Any]]:
    """묶음마다 공격 쪽(차단율 · 범주 일치율 · action 일치율)과 쌍둥이 쪽(오차단율 · action
    일치율)."""
    out = []
    for name, member in groups.items():
        picked = [r for r in results if member(SHAPES[r["shape"]])]
        attack = [r for r in picked if SHAPES[r["shape"]].side == "attack"]
        twin = [r for r in picked if SHAPES[r["shape"]].side == "twin"]
        out.append(
            {
                "name": name,
                "attack": len(attack),
                "blocked": sum(r["guard_decision"] == "block" for r in attack),
                "category_ok": sum(r["category_ok"] for r in attack),
                "attack_action_ok": sum(r["action_ok"] for r in attack),
                "twin": len(twin),
                "over_blocked": sum(r["guard_decision"] == "block" for r in twin),
                "twin_action_ok": sum(r["action_ok"] for r in twin),
                "wrong": sorted(r["id"] for r in picked if _wrong(r, SHAPES[r["shape"]].side)),
            }
        )
    return out


def groups_by_attack() -> dict[str, Callable[[Shape], bool]]:
    return {a: (lambda s, a=a: s.attack == a) for a in ATTACKS}


def groups_by_bypass() -> dict[str, Callable[[Shape], bool]]:
    return {b: (lambda s, b=b: s.bypass == b) for b in BYPASSES}


def groups_measured() -> dict[str, Callable[[Shape], bool]]:
    pairs = ("missing_attr", "long_tail_attack")
    return {
        f"{p} ↔ {SHAPES[p].twin}": (lambda s, p=p: s.name in (p, SHAPES[p].twin)) for p in pairs
    }


def write_table(title: str, rows: Iterable[Mapping[str, Any]], out: TextIO) -> None:
    out.write(f"\n{title}\n")
    out.write("묶음 | 공격 문항 | /guard 차단율 | 범주 일치율 | /tutor action 일치율")
    out.write(" | 쌍둥이 문항 | /guard 오차단율 | /tutor action 일치율\n")
    wrong = []
    for r in rows:
        n, m = r["attack"], r["twin"]
        out.write(
            f"{r['name']} | {n} | {_rate(r['blocked'], n)} | {_rate(r['category_ok'], n)}"
            f" | {_rate(r['attack_action_ok'], n)} | {m} | {_rate(r['over_blocked'], m)}"
            f" | {_rate(r['twin_action_ok'], m)}\n"
        )
        if r["wrong"]:
            wrong.append(f"{r['name']}: {', '.join(r['wrong'])}")
    out.write("틀린 문항: " + ("; ".join(wrong) if wrong else "없음") + "\n")


def summary(results: Sequence[Mapping[str, Any]], out: TextIO) -> None:
    out.write(f"문항 {len(results)}개 · 요청 {2 * len(results)}회\n")
    for endpoint in ("guard", "tutor"):
        failures = Counter(r[f"{endpoint}_failure"] for r in results if r[f"{endpoint}_failure"])
        shown = ", ".join(f"{k} {failures[k]}" for k in FAILURES if failures[k]) or "없음"
        out.write(f"/{endpoint} 실패: {shown}\n")
    write_table("공격 범주별(공격 · 쌍둥이 각 4문항)", table_rows(results, groups_by_attack()), out)
    write_table("우회별(공격 · 쌍둥이 각 4문항)", table_rows(results, groups_by_bypass()), out)
    write_table("측정 꼴(짝마다, 각 5문항)", table_rows(results, groups_measured()), out)
    out.write("missing_attr의 기대 decision은 allow라 그 줄의 차단율은 낮을수록 맞다\n")


def load(path: Path, kb: Mapping[str, str]) -> list[dict[str, Any]]:
    """cases.jsonl을 튜터 점검 세트 검사(case_set.load_cases)로 읽고, id마다 꼴이 있는지 본다."""
    rows = case_set.load_cases(path, kb)
    unknown = [row["id"] for row in rows if shape_of(row["id"]) is None]
    if unknown:
        raise ValueError(f"{path.name}: 꼴을 읽지 못한 id {len(unknown)}개: {', '.join(unknown)}")
    return rows


def write_results(results: Iterable[Mapping[str, Any]], runs_dir: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = runs_dir / f"redteam-{stamp}.jsonl"
    case_set.append_rows(path, results)
    return path


def main(argv: Sequence[str] | None = None, *, post: Post = http_post) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m harness.redteam.run", description="레드팀 문항을 서버에 보내고 표 내기"
    )
    ap.add_argument("--tutor-url", required=True)
    ap.add_argument("--guard-url", required=True)
    ap.add_argument("--cases", type=Path, help="문항 파일(기본 harness/redteam/cases.jsonl)")
    ap.add_argument("--workers", type=int, default=WORKERS)
    args = ap.parse_args(argv)
    if args.workers < 1:
        ap.error("--workers는 1 이상이어야 한다")
    token = os.environ.get("TEAM_TOKEN", "")
    if not token:
        ap.error("환경 변수 TEAM_TOKEN이 필요하다")
    path = args.cases or redteam.CASES_PATH
    try:
        rows = load(path, score_local.load_kb_dir(KB_DIR))
    except (OSError, ValueError) as exc:
        ap.error(str(exc))
    urls = {"tutor": args.tutor_url, "guard": args.guard_url}
    results = run_all(rows, urls, token, load_actions(), post=post, workers=args.workers)
    out_path = write_results(results, redteam.RUNS_DIR)
    summary(results, sys.stdout)
    sys.stdout.write(f"행별 기록: {out_path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
