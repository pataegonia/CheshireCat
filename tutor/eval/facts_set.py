"""사실 손실 세트(측정 전용, API 0회).

`python -m tutor.eval.facts_set --ids <id 파일> --out <경로>`.

2차 자체 검증처럼 normal answer 문장을 바꾸는 실험을 빨리 거르는 공개 부분 세트다. id 파일의 행
(동결 후보 판정 실행에서 normal 사실을 잃은 행)과, 교차 검증의 정답 normal·answer 행 중 id 파일에
없는 행에서 CONTROLS개(대조군, SEED와 id 해시 순서라 언제 만들어도 같다)를 고른다. 공개 파일의
줄을 그대로 옮겨 `run_local --cases <경로>`가 부분 세트로 잰다. 화면에는 수와 경로만 낸다.
"""

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from tutor.eval.grader import PUBLIC_DIR
from tutor.eval.run_local import holdout_ids

SEED = "facts-set-v1"
CONTROLS = 24


def read_ids(path: Path) -> list[str]:
    """한 줄에 id 하나. #부터 줄 끝까지는 주석."""
    lines = (
        line.split("#", 1)[0].strip() for line in path.read_text(encoding="utf-8").splitlines()
    )
    return [line for line in lines if line]


def build(
    ids: Sequence[str],
    out: Path,
    *,
    controls: int = CONTROLS,
    public: Path = PUBLIC_DIR / "tutor.jsonl",
) -> tuple[int, int]:
    """(손실 행 수, 대조 행 수). 홀드아웃이거나 공개 세트에 없는 id면 ValueError."""
    lines = [line for line in public.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = [json.loads(line) for line in lines]
    held = holdout_ids(rows)
    known = {row["id"] for row in rows}
    wanted = set(ids)
    bad = sorted(wanted - known) + sorted(wanted & held)
    if bad:
        raise ValueError(f"공개 교차 검증 행이 아닌 id: {bad}")
    pool = [
        row["id"]
        for row in rows
        if row["id"] not in held
        and row["id"] not in wanted
        and (row["labels"]["category"], row["labels"]["action"]) == ("normal", "answer")
    ]
    order = sorted(pool, key=lambda r: hashlib.sha256(f"{SEED}:{r}".encode()).hexdigest())
    picked = wanted | set(order[:controls])
    kept = [line for line, row in zip(lines, rows, strict=True) if row["id"] in picked]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(f"{line}\n" for line in kept), encoding="utf-8", newline="\n")
    return len(wanted), len(picked) - len(wanted)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="사실 손실 행 + 대조 normal 행 부분 세트")
    ap.add_argument("--ids", type=Path, required=True, help="손실 행 id 파일(한 줄에 하나)")
    ap.add_argument("--out", type=Path, required=True, help="쓸 JSONL 경로(공개 파일과 같은 줄)")
    ap.add_argument("--controls", type=int, default=CONTROLS)
    args = ap.parse_args(argv)
    if args.controls < 0:
        ap.error("--controls는 0 이상")
    try:
        lost, ctl = build(read_ids(args.ids), args.out, controls=args.controls)
    except (OSError, ValueError) as exc:
        ap.error(str(exc))
    sys.stdout.write(f"사실 손실 세트 {lost + ctl}행(손실 {lost}, 대조 {ctl}): {args.out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
