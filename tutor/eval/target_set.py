"""목표 세트(측정 전용, API 0회). `python -m tutor.eval.target_set --out <경로>`.

교차 검증 행(공개 튜터 행 중 홀드아웃 밖) 가운데 정답 abstain 행 전부와, 정답 answer·범주 normal
행을 과목(정답 근거 섹션 ID 앞부분 PY·PR·FIN)마다 PER_SUBJECT개씩 고른다. 고르는 순서는 SEED와
행 id의 해시라 언제 만들어도 같다. 공개 파일의 줄을 그대로 옮겨 `run_local --cases <경로>`가 공개
부분 세트로 잰다. 화면에는 수와 경로만 낸다.
"""

import argparse
import hashlib
import json
import sys
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Any

from tutor.eval.grader import PUBLIC_DIR
from tutor.eval.run_local import holdout_ids

SEED = "target-set-v1"
SUBJECTS = ("PY", "PR", "FIN")
PER_SUBJECT = 10


def subject(row: dict[str, Any]) -> str | None:
    """정답 근거 첫 섹션 ID의 과목(PY·PR·FIN). 근거가 없으면 None."""
    evidence = row["labels"].get("evidence") or []
    return evidence[0].split("-", 1)[0] if evidence else None


def _order(row_id: str) -> str:
    return hashlib.sha256(f"{SEED}:{row_id}".encode()).hexdigest()


def pick(rows: Sequence[dict[str, Any]], held: Collection[str]) -> set[str]:
    """교차 검증 행 중 정답 abstain 전부 + 과목마다 정답 answer·normal PER_SUBJECT개의 id."""
    cv = [row for row in rows if row["id"] not in held]
    picked = {row["id"] for row in cv if row["labels"]["action"] == "abstain"}
    normal = [
        row
        for row in cv
        if row["labels"]["action"] == "answer" and row["labels"]["category"] == "normal"
    ]
    for name in SUBJECTS:
        pool = sorted((row["id"] for row in normal if subject(row) == name), key=_order)
        if len(pool) < PER_SUBJECT:
            raise ValueError(f"{name} 정답 answer·normal 교차 검증 행이 {len(pool)}개뿐이다")
        picked.update(pool[:PER_SUBJECT])
    return picked


def build(out: Path, public: Path = PUBLIC_DIR / "tutor.jsonl") -> dict[str, int]:
    """고른 행을 공개 파일 순서·원래 줄 그대로 out에 쓴다. 돌려주는 것: 묶음별 행 수."""
    lines = [line for line in public.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = [json.loads(line) for line in lines]
    picked = pick(rows, holdout_ids(rows))
    kept = [line for line, row in zip(lines, rows, strict=True) if row["id"] in picked]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(f"{line}\n" for line in kept), encoding="utf-8", newline="\n")
    counts = {"abstain": 0, **dict.fromkeys(SUBJECTS, 0)}
    for row in rows:
        if row["id"] in picked:
            key = "abstain" if row["labels"]["action"] == "abstain" else subject(row)
            counts[key] += 1
    return counts


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="교차 검증 목표 세트(abstain 전부 + 과목별 normal)")
    ap.add_argument("--out", type=Path, required=True, help="쓸 JSONL 경로(공개 파일과 같은 줄)")
    args = ap.parse_args(argv)
    counts = build(args.out)
    shown = ", ".join(f"{name} {n}" for name, n in counts.items())
    sys.stdout.write(f"목표 세트 {sum(counts.values())}행({shown}): {args.out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
