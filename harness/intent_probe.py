"""T 의도 판정 모의(#209 프롬프트, 측정 전용). 저장소 루트에서 `python -m harness.intent_probe`.

공개 튜터 교차 검증 160행에 T(tutor/prompts/intent.md)를 1회씩 부르고, 기준 실행 결과 JSON들의
행별 주 호출 action 위에 "T가 위기·탈옥·유해·무관 결과물·대행·정답만·저신호라고 하면 그 action으로
덮기"를 모의한다. 서버 코드는 바꾸지 않는다.

- 입력: 과목 목차(KB 섹션 ID·제목), 신호 힌트(주 호출과 같은 이름), 대화(주 호출과 같은 블록),
  끝 요청(마지막 발화가 300자 이상이면 끝 두 문장). 출력: label, self_info(json_schema strict).
- 덮기 규칙(모의) 네 가지를 함께 낸다: 강할 때만(escalate → refuse → redirect → clarify →
  scaffold → abstain → answer 순서로 T 쪽이 앞설 때만) · 항상, 각각 탈옥은 지시 무력화 신호가
  있을 때만 덮는 변형(jailbreak_needs_signal과 같은 생각). 학습 질문이면 덮지 않는다.
- 연속 429가 3번이면 뒤 행은 보내지 않는다. `--records`로 이미 부른 기록을 주면 API 0회.
- 기록: harness/runs/intent-<시각>.json(git 제외). 행 id·정답·신호·이름표만(본문 없음).
"""

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from harness.oom_probe import cv_rows
from tutor.eval import metrics
from tutor.judge.conversation import tidy
from tutor.judge.flow import _messages
from tutor.judge.main_call import _turn_block, defuse
from tutor.judge.signals import Signal, detect, labels
from tutor.request import parse_request
from tutor.runtime.config import load_config
from tutor.runtime.llm import TutorLLM, missing_api_env
from tutor.startup import prepare

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
PROMPT_PATH = REPO / "tutor" / "prompts" / "intent.md"
RUNS_DIR = ROOT / "runs"
ROLE = "intent"
LONG_TARGET = 300  # 마지막 발화가 이 글자 수 이상이면 끝 요청을 따로 보인다
CV_ROWS = 160
RATE_LIMIT_STOP = 3  # 429가 연달아 이만큼이면 뒤 행은 보내지 않는다

Label = Literal[
    "위기", "탈옥", "유해-실행", "유해-비하", "무관 결과물", "제출물 대행", "정답만 요구", "저신호",
    "학습 질문",
]  # fmt: skip
ACTION_OF: dict[str, str | None] = {
    "위기": "escalate",
    "탈옥": "refuse",
    "유해-실행": "refuse",
    "유해-비하": "redirect",
    "무관 결과물": "redirect",
    "제출물 대행": "scaffold",
    "정답만 요구": "scaffold",
    "저신호": "clarify",
    "학습 질문": None,
}
ORDER = ("escalate", "refuse", "redirect", "clarify", "scaffold", "abstain", "answer")
_SENT_END = re.compile(r"(?<=[.!?。？！])\s+")


class IntentOut(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    label: Label
    self_info: bool


def load_system(path: Path = PROMPT_PATH) -> str:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    first, _, rest = text.partition("\n")
    if not (first.startswith("<!--") and first.endswith("-->")):
        raise ValueError(f"{path.name} 첫 줄에 출처 주석이 없다")
    return rest.strip("\n")


def table_of_contents(sections: Mapping[str, Any]) -> str:
    lines = [f"- {sid} {s.title}" for sid, s in sorted(sections.items())]
    return "<과목 목차>\n" + "\n".join(lines) + "\n</과목 목차>"


def end_request(text: str) -> str | None:
    if len(text) < LONG_TARGET:
        return None
    parts = [p for p in _SENT_END.split(text.strip()) if p.strip()]
    return " ".join(parts[-2:]) if parts else None


def build_user(toc: str, row: Mapping[str, Any]) -> tuple[str, list[str]]:
    req = parse_request({"messages": row["messages"]})
    if req is None:
        raise ValueError(f"{row['id']}: 요청을 읽지 못했다")
    conv = tidy(_messages(req))
    signals = detect(conv)
    hint = ", ".join(labels(signals)) if signals else "없음"
    parts = [toc, f"<신호>{defuse(hint)}</신호>", _turn_block(conv)]
    target = next((t.text for t in conv.window if t.is_target), "")
    tail = end_request(target)
    if tail:
        parts.append(f"<끝 요청>{defuse(tail)}</끝 요청>")
    return "\n\n".join(parts), [s.value for s in signals]


def stronger(a: str, b: str | None) -> bool:
    """a가 b보다 앞선(강한) action인가. b가 None(형식 틀림)이면 참."""
    if b not in ORDER:
        return True
    return ORDER.index(a) < ORDER.index(b)


def override(rec: Mapping[str, Any], main: str | None, *, only_stronger: bool, needs_signal: bool):
    """T 이름표로 덮은 action. 덮지 않으면 None."""
    label = rec.get("label")
    new = ACTION_OF.get(label) if label is not None else None
    if new is None or new == main:
        return None
    if needs_signal and label == "탈옥" and Signal.OVERRIDE.value not in rec["signals"]:
        return None
    if only_stronger and not stronger(new, main):
        return None
    return new


def simulate(
    records: Mapping[str, Mapping[str, Any]],
    runs: Sequence[Mapping[str, Mapping[str, Any]]],
    *,
    only_stronger: bool,
    needs_signal: bool,
) -> dict[str, Any]:
    """기준 실행마다 덮기를 적용해 맞게·틀리게 바뀐 수의 실행당 평균과 정답 범주별 합."""
    tot: Counter[str] = Counter()
    by_cat: dict[str, Counter[str]] = {}
    for emitted in runs:
        for rid, em in emitted.items():
            rec = records.get(rid)
            if rec is None:
                continue
            main = em.get("action")
            new = override(rec, main, only_stronger=only_stronger, needs_signal=needs_signal)
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


def _meta(item: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("id", "gold_category", "gold_action", "signals")
    return {k: item[k] for k in keys}


async def call_all(llm: TutorLLM, system: str, items: list[dict[str, Any]], timeout_s: float):
    gate = asyncio.Semaphore(metrics.WORKERS)
    streak = {"429": 0}

    async def one(item: dict[str, Any]) -> dict[str, Any]:
        async with gate:
            if streak["429"] >= RATE_LIMIT_STOP:
                return {**_meta(item), "label": None, "self_info": None, "error": "not_sent",
                        "finish_reason": None, "latency_ms": 0.0}  # fmt: skip
            loop = asyncio.get_running_loop()
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": item["user"]},
            ]
            result = await llm.complete(
                ROLE, messages, IntentOut, deadline=loop.time() + timeout_s, timeout_s=timeout_s,
                retries=0,
            )  # fmt: skip
        streak["429"] = streak["429"] + 1 if result.error == "rate_limit" else 0
        ok = result.error is None and isinstance(result.parsed, IntentOut)
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
    runs = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        runs.extend(data["report"]["cv_emitted_by_run"])
    return runs


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="T 의도 판정 모의(측정 전용)")
    ap.add_argument(
        "--main", type=Path, nargs="+", required=True, help="기준 run_local 결과 JSON들"
    )
    ap.add_argument("--effort", choices=("low", "medium"), default="low")
    ap.add_argument("--timeout-s", type=float, default=30.0)
    ap.add_argument("--records", type=Path, help="이미 부른 기록 JSON(주면 API를 부르지 않는다)")
    ap.add_argument("--out", type=Path, default=RUNS_DIR)
    args = ap.parse_args(argv)
    runs = load_runs(args.main)
    out = sys.stdout
    if args.records is not None:
        records = json.loads(args.records.read_text(encoding="utf-8"))["rows"]
    else:
        missing = missing_api_env()
        if missing:
            ap.error(f"운영진 API 환경변수가 필요하다: {'·'.join(missing)}")
        rows = cv_rows()
        if len(rows) != CV_ROWS:
            ap.error(f"교차 검증 행이 {len(rows)}개다({CV_ROWS}개여야 한다)")
        cfg = load_config(overrides={"llm.reasoning_effort": args.effort})
        state = SimpleNamespace()
        prepare(state, cfg)
        llm: TutorLLM = state.tutor_llm
        llm.max_calls = len(rows)
        items = make_items(rows, table_of_contents(state.tutor_kb.sections))  # 서버와 같은 KB
        out.write(f"T 호출 {len(items)}회(재시도 없음, 동시 {metrics.WORKERS})\n")
        records = asyncio.run(call_all(llm, load_system(), items, args.timeout_s))
        path = write_records(records, args.out, args.effort)
        out.write(f"결과: {path.as_posix()}\n")
    show(records, runs, out)
    return 0


def make_items(rows: Sequence[Mapping[str, Any]], toc: str) -> list[dict[str, Any]]:
    items = []
    for row in rows:
        user, sig = build_user(toc, row)
        gold = row["labels"]
        items.append({
            "id": row["id"], "user": user, "signals": sig,
            "gold_category": gold["category"], "gold_action": gold["action"],
        })  # fmt: skip
    return items


def write_records(records: Sequence[Mapping[str, Any]], out_dir: Path, effort: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = out_dir / f"intent-{effort}-{stamp}.json"
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
    by_id = {r["id"]: r for r in records}
    out.write(f"덮기 모의(기준 {len(runs)}회, right·wrong·other·net은 실행당 평균, 범주별은 합):\n")
    for only in (True, False):
        for needs in (False, True):
            sim = simulate(by_id, runs, only_stronger=only, needs_signal=needs)
            name = ("강할 때만" if only else "항상") + (" · 탈옥은 신호 있을 때만" if needs else "")
            out.write(f"- {name}: {json.dumps(sim, ensure_ascii=False)}\n")


if __name__ == "__main__":
    raise SystemExit(main())
