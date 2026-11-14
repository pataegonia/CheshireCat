"""개발용: 신호별 정밀도·재현율 보고(TU-05 수용 기준). 서버는 이 파일을 읽지 않는다.

    PYTHONPATH=. uv run --project guardrail --no-sync python -m tutor.dev_only.signal_report

- 판단 대상(마지막 학습자 발화)에 신호를 걸고, 신호마다 짝이 되는 범주를 양성으로 본다.
- 규칙은 학습하지 않으므로 교차 검증 폴드와 상관없이 같은 값이다. 단, 공개 세트를 보며 쓴
  규칙이라 표본 안(in-sample) 값이다. 비공개 일반화는 팀 작성 점검 세트(TU-19)로 본다.
- 오차단 위험: 정답 action이 answer·scaffold인 행 중 공격 신호(지시 무력화)가 걸린 행 수.
- 출력은 집계 수치뿐이다(본문을 화면에 내지 않음). `--show` 를 주면 틀린 행의 id만 낸다.
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from tutor.judge.conversation import tidy
from tutor.judge.signals import Signal, detect, direct_clarify

ROOT = Path(__file__).resolve().parents[2]
POSITIVE = {
    Signal.CRISIS: {"emotional_distress"},
    Signal.OVERRIDE: {"jailbreak_injection"},
    Signal.ANSWER_ONLY: {"answer_seeking"},
    Signal.DELEGATION: {"academic_dishonesty"},
    Signal.SELF_PII: {"pii_disclosure"},
    Signal.GRADING: set(),  # 공개 세트에 범주 없음(채점 조작은 jailbreak 쪽 변형으로 본다)
}


def load(name: str) -> list[dict]:
    path = ROOT / "competition" / "public" / f"{name}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def report(rows: list[dict], show: bool) -> list[str]:
    out = []
    hits = {s: Counter() for s in Signal}
    wrong: dict[Signal, list[str]] = {s: [] for s in Signal}
    over_block = 0
    low_tp = low_fp = low_fn = 0
    for row in rows:
        conv = tidy(row["messages"])
        category = row["labels"]["category"]
        found = set(detect(conv))
        for s in Signal:
            pos = category in POSITIVE[s]
            key = ("tp" if pos else "fp") if s in found else ("fn" if pos else "tn")
            hits[s][key] += 1
            if key in ("fp", "fn"):
                wrong[s].append(f"{key}:{row['id']}")
        if Signal.OVERRIDE in found and row["labels"].get("action") in ("answer", "scaffold"):
            over_block += 1
        low = direct_clarify(conv)
        is_low = category == "low_signal_input"
        low_tp += low and is_low
        low_fp += low and not is_low
        low_fn += (not low) and is_low
    out.append(f"{'signal':<12} {'tp':>4} {'fp':>4} {'fn':>4} {'precision':>9} {'recall':>7}")
    for s in Signal:
        h = hits[s]
        prec = h["tp"] / (h["tp"] + h["fp"]) if h["tp"] + h["fp"] else float("nan")
        rec = h["tp"] / (h["tp"] + h["fn"]) if h["tp"] + h["fn"] else float("nan")
        out.append(f"{s.value:<12} {h['tp']:>4} {h['fp']:>4} {h['fn']:>4} {prec:>9.3f} {rec:>7.3f}")
        if show and wrong[s]:
            out.append("    " + " ".join(wrong[s]))
    out.append(f"direct_clarify: tp {low_tp} fp {low_fp} fn {low_fn}")
    out.append(f"override 신호가 걸린 answer·scaffold 행(오차단 위험): {over_block}")
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--set", choices=["tutor", "guard", "both"], default="both")
    p.add_argument("--show", action="store_true", help="틀린 행의 id")
    args = p.parse_args()
    names = ["tutor", "guard"] if args.set == "both" else [args.set]
    for name in names:
        rows = load(name)
        sys.stdout.write(f"## {name} ({len(rows)}행)\n")
        sys.stdout.write("\n".join(report(rows, args.show)) + "\n")


if __name__ == "__main__":
    main()
