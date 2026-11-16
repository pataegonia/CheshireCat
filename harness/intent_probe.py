"""T 의도 판정 모의(#209 프롬프트, 측정 전용). 저장소 루트에서 `python -m harness.intent_probe`.

공개 튜터 교차 검증 160행(또는 `--cases team` 점검 세트)에 T(tutor/prompts/intent.md)를 1회씩
부르고, 기준 실행 결과 JSON들의 행별 주 호출 action 위에 T 이름표로 덮기를 모의한다. 서버 코드는
바꾸지 않는다. 메시지와 덮기 규칙은 서버(`tutor.judge.intent`)와 같은 함수를 쓴다.

- 덮기 규칙: 서버 규칙(`intent.decide`)과 비교용 네 가지(강할 때만 · 항상, 각각 탈옥은 지시 무력화
  신호가 있을 때만)를 함께 낸다. 비교용은 학습 질문만 덮지 않는다.
- `--records A.json [B.json …]`이면 API 0회로 기록마다 모의를 내고, 둘 이상이면 행별 이름표가
  기록끼리 같은 비율(흔들림)을 낸다. `--main`이 없으면 이름표 분포만 낸다(점검 세트 등).
- 연속 429가 3번이면 뒤 행은 보내지 않는다.
- 기록: harness/runs/intent-<시각>.json(git 제외). 행 id·정답·신호·이름표만(본문 없음).
"""

import argparse
import asyncio
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from harness.oom_probe import cv_rows
from tutor.answer.templates import template_for
from tutor.eval import case_set, metrics
from tutor.judge import intent
from tutor.judge.conversation import tidy
from tutor.judge.flow import _messages
from tutor.judge.signals import Signal, detect
from tutor.request import parse_request
from tutor.runtime.config import load_config
from tutor.runtime.llm import TutorLLM, missing_api_env
from tutor.startup import prepare

ROOT = Path(__file__).resolve().parent
RUNS_DIR = ROOT / "runs"
CV_ROWS = 160
RATE_LIMIT_STOP = 3  # 429가 연달아 이만큼이면 뒤 행은 보내지 않는다
# 비교용 규칙의 이름표 → action(서버 규칙은 intent.decide)
ACTION_OF: dict[str, str | None] = {k: (v[1] if v else None) for k, v in intent.TARGET.items()}


def build_user(system: str, toc: str, row: Mapping[str, Any]) -> tuple[list[dict], list[str]]:
    req = parse_request({"messages": row["messages"]})
    if req is None:
        raise ValueError(f"{row['id']}: 요청을 읽지 못했다")
    conv = tidy(_messages(req))
    signals = detect(conv)
    return intent.build_messages(system, toc, conv, signals), [s.value for s in signals]


def stronger(a: str, b: str | None) -> bool:
    """a가 b보다 앞선(강한) action인가. b가 None(형식 틀림)이면 참."""
    if b not in intent.ORDER:
        return True
    return intent.ORDER.index(a) < intent.ORDER.index(b)


def override(
    rec: Mapping[str, Any], em: Mapping[str, Any], *, only_stronger: bool, needs_signal: bool
):
    """비교용 규칙으로 덮은 action. 덮지 않으면 None. 기준 실행이 낸 action을 그대로 본다."""
    main = em.get("action")
    label = rec.get("label")
    new = ACTION_OF.get(label) if label is not None else None
    if new is None or new == main:
        return None
    if needs_signal and label == "탈옥" and Signal.OVERRIDE.value not in rec["signals"]:
        return None
    if only_stronger and not stronger(new, main):
        return None
    return new


def server_override(rec: Mapping[str, Any], em: Mapping[str, Any]) -> str | None:
    """서버 규칙. 주 호출이 실패한 행(main_failed)은 서버처럼 고정 문장 조합으로만 덮는다."""
    signals = [Signal(v) for v in rec["signals"]]
    decided = intent.decide(rec.get("label"), signals, em.get("action"))
    if decided is None:
        return None
    if em.get("main_failed") and template_for(decided.category, decided.action) is None:
        return None
    return decided.action


def simulate(
    records: Mapping[str, Mapping[str, Any]],
    runs: Sequence[Mapping[str, Mapping[str, Any]]],
    rule: Any,
) -> dict[str, Any]:
    """기준 실행마다 덮기를 적용해 맞게·틀리게 바뀐 수의 실행당 평균과 정답 범주별 합.

    `rule(rec, em)`은 덮은 action이나 None이다(em은 기준 실행이 그 행에서 낸 것)."""
    tot: Counter[str] = Counter()
    by_cat: dict[str, Counter[str]] = {}
    for emitted in runs:
        for rid, em in emitted.items():
            rec = records.get(rid)
            if rec is None:
                continue
            main = em.get("action")
            new = rule(rec, em)
            if new is None:
                continue
            gold = rec["gold_action"]
            kind = "right" if new == gold else "wrong" if main == gold else "other"
            tot[kind] += 1
            by_cat.setdefault(rec["gold_category"], Counter())[kind] += 1
    n = max(1, len(runs))
    out: dict[str, Any] = {k: round(tot[k] / n, 2) for k in ("right", "wrong", "other")}
    out["net"] = round(out["right"] - out["wrong"], 2)
    out["by_category"] = {c: dict(v) for c, v in sorted(by_cat.items())}
    return out


def rules() -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = [("서버 규칙(intent.decide)", server_override)]
    for only in (True, False):
        for needs in (False, True):
            name = ("강할 때만" if only else "항상") + (" · 탈옥은 신호 있을 때만" if needs else "")

            def rule(rec, em, only=only, needs=needs):
                return override(rec, em, only_stronger=only, needs_signal=needs)

            out.append((name, rule))
    return out


def _meta(item: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("id", "gold_category", "gold_action", "signals")
    return {k: item[k] for k in keys}


async def call_all(llm: TutorLLM, items: list[dict[str, Any]], timeout_s: float):
    gate = asyncio.Semaphore(metrics.WORKERS)
    streak = {"429": 0}

    async def one(item: dict[str, Any]) -> dict[str, Any]:
        async with gate:
            if streak["429"] >= RATE_LIMIT_STOP:
                return {**_meta(item), "label": None, "self_info": None, "error": "not_sent",
                        "finish_reason": None, "latency_ms": 0.0}  # fmt: skip
            loop = asyncio.get_running_loop()
            result = await llm.complete(
                intent.ROLE, item["messages"], intent.IntentOut,
                deadline=loop.time() + timeout_s, timeout_s=timeout_s, retries=0,
            )  # fmt: skip
        streak["429"] = streak["429"] + 1 if result.error == "rate_limit" else 0
        ok = result.error is None and isinstance(result.parsed, intent.IntentOut)
        return {
            **_meta(item),
            "label": result.parsed.label if ok else None,
            "self_info": result.parsed.self_info if ok else None,
            "error": result.error,
            "finish_reason": result.finish_reason,
            "latency_ms": round(result.latency_ms, 1),
        }

    return await asyncio.gather(*(one(i) for i in items))


def load_runs(paths: Sequence[Path]) -> list[dict[str, Any]]:
    """기준 실행마다 행 → 낸 것. row_trace가 있으면 주 호출 실패 행에 main_failed를 붙인다."""
    runs = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        traces = (data.get("row_trace") or {}).get("by_run") or []
        for number, emitted in enumerate(data["report"]["cv_emitted_by_run"]):
            trace = traces[number] if number < len(traces) else {}
            runs.append({
                rid: {**em, "main_failed": trace.get(rid, {}).get("main_call") == "failed"}
                for rid, em in emitted.items()
            })  # fmt: skip
    return runs


def make_items(rows: Sequence[Mapping[str, Any]], system: str, toc: str) -> list[dict[str, Any]]:
    items = []
    for row in rows:
        messages, sig = build_user(system, toc, row)
        gold = row["labels"]
        items.append({
            "id": row["id"], "messages": messages, "signals": sig,
            "gold_category": gold["category"], "gold_action": gold["action"],
        })  # fmt: skip
    return items


def write_records(records: Sequence[Mapping[str, Any]], out_dir: Path, name: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = out_dir / f"intent-{name}-{stamp}.json"
    text = json.dumps({"rows": list(records)}, ensure_ascii=False, indent=1)
    path.write_text(text, encoding="utf-8")
    return path


def show(records: Sequence[Mapping[str, Any]], runs: Sequence[Any], out: Any) -> None:
    fails = Counter(r["error"] for r in records if r.get("label") is None)
    lat = sorted(r["latency_ms"] for r in records if r.get("label") is not None)
    if lat:
        p50, p95 = lat[len(lat) // 2] / 1000, lat[min(len(lat) - 1, int(len(lat) * 0.95))] / 1000
        out.write(f"T 실패 {dict(fails)} · 지연 p50 {p50:.2f} · p95 {p95:.2f}초\n")
    conf: dict[str, Counter] = {}
    for r in records:
        conf.setdefault(r["gold_category"], Counter())[r.get("label")] += 1
    out.write("정답 범주 → T 이름표:\n")
    for cat in sorted(conf):
        out.write(f"- {cat}: " + ", ".join(f"{k} {v}" for k, v in conf[cat].most_common()) + "\n")
    if not runs:
        return
    by_id = {r["id"]: r for r in records}
    out.write(f"덮기 모의(기준 {len(runs)}회, right·wrong·other·net은 실행당 평균, 범주별은 합):\n")
    for name, rule in rules():
        sim = simulate(by_id, runs, rule)
        out.write(f"- {name}: {json.dumps(sim, ensure_ascii=False)}\n")


def agreement(sets: Sequence[Sequence[Mapping[str, Any]]]) -> tuple[int, int]:
    """(모든 기록에서 이름표가 같은 행 수, 모든 기록에 있는 행 수). 실패(None)는 다르다고 본다."""
    maps = [{r["id"]: r.get("label") for r in s} for s in sets]
    shared = set.intersection(*(set(m) for m in maps))
    same = sum(1 for rid in shared if len({m[rid] for m in maps}) == 1 and maps[0][rid])
    return same, len(shared)


def _rows(cases: str) -> list[dict[str, Any]]:
    if cases == "public":
        return cv_rows()
    return case_set.load_for_run(cases)


def _call(args: argparse.Namespace, ap: argparse.ArgumentParser) -> list[dict[str, Any]]:
    missing = missing_api_env()
    if missing:
        ap.error(f"운영진 API 환경변수가 필요하다: {'·'.join(missing)}")
    rows = _rows(args.cases)
    if args.cases == "public" and len(rows) != CV_ROWS:
        ap.error(f"교차 검증 행이 {len(rows)}개다({CV_ROWS}개여야 한다)")
    cfg = load_config(overrides={"llm.reasoning_effort": args.effort})
    state = SimpleNamespace()
    prepare(state, cfg)  # 서버와 같은 KB 색인·LLM 호출기
    llm: TutorLLM = state.tutor_llm
    llm.max_calls = len(rows)  # 행마다 1회를 넘지 않는다
    toc = intent.table_of_contents(state.tutor_kb.sections)
    items = make_items(rows, intent.load_system(), toc)
    sys.stdout.write(f"T 호출 {len(items)}회(재시도 없음, 동시 {metrics.WORKERS})\n")
    records = asyncio.run(call_all(llm, items, args.timeout_s))
    name = args.effort if args.cases == "public" else f"{args.effort}-team"
    path = write_records(records, args.out, name)
    sys.stdout.write(f"결과: {path.as_posix()}\n")
    return records


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="T 의도 판정 모의(측정 전용)")
    ap.add_argument("--main", type=Path, nargs="+", default=[], help="기준 run_local 결과 JSON들")
    ap.add_argument("--cases", default="public", help="public(기본) | team | 점검 세트 JSONL 경로")
    ap.add_argument("--effort", choices=("low", "medium"), default="low")
    ap.add_argument("--timeout-s", type=float, default=30.0)
    ap.add_argument("--records", type=Path, nargs="+", help="이미 부른 기록 JSON들(API 0회)")
    ap.add_argument("--out", type=Path, default=RUNS_DIR)
    args = ap.parse_args(argv)
    if args.timeout_s <= 0:
        ap.error("--timeout-s는 0보다 커야 한다")
    runs = load_runs(args.main)
    out = sys.stdout
    if args.records is None:
        show(_call(args, ap), runs, out)
        return 0
    sets = [json.loads(p.read_text(encoding="utf-8"))["rows"] for p in args.records]
    for path, records in zip(args.records, sets, strict=True):
        out.write(f"== {path.name}\n")
        show(records, runs, out)
    if len(sets) > 1:
        same, total = agreement(sets)
        out.write(f"기록 {len(sets)}개에서 이름표가 모두 같은 행 {same}/{total}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
