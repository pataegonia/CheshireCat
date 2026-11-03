"""근거 평가 세트 조립 (GP1 S1.1).

GP0 준비분: 결정적인 부분(하위 분할·인용 바꿔치기·조립)과 CLI 골격.

하위 명령: answers(모의 튜터 답변, API) · inject(다른 계열 주입, API) · cite(인용 바꿔치기) ·
review(사람 확인용 diff) · assemble(합치기·중복 제거·분할·통계).
- API를 쓰는 answers·inject는 eval.run처럼 --estimate와 --max-calls를 받고, --estimate가 아니면
  --max-calls가 필수다. 본체는 GP1 S1.1에서 채운다(지금은 사전 확인만 하고 종료 2).
- cite의 문장 고르기(원래 청크와 3-gram 포함률 ≥0.7, 바꿀 청크와 <0.3)는 S1.1의
  prefilter.containment()가 생긴 뒤 붙인다. 지금은 바꿔치기 규칙만 결정적으로 고정한다.
"""

import argparse
import hashlib
import re
import sys
from collections import Counter
from typing import Any, Literal

from guardrail.eval.datasets import Item, _bucket, dedupe

SUBSPLIT_SALT = "cheshire-subsplit:"
MISSING_SID = "S9"  # 없는 sid로 바꾼 인용 오류 20건(GR-07 확인용)
_CITE = re.compile(r"\[(S\d+)\]")


def subsplit(seed_id: str) -> Literal["calibration", "report"]:
    """master-plan §4.6: 씨앗 해시 값이 60 미만이면 calibration(보정), 아니면 report(보고)."""
    return "calibration" if _bucket(SUBSPLIT_SALT, seed_id) < 60 else "report"


def _pick(key: str, n: int) -> int:
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) % n


def swap_citation(
    answer: str, sids: list[str], key: str, *, missing: bool = False
) -> tuple[str, str, str]:
    """답변의 인용 하나를 바꾼다. (새 답변, 원래 sid, 새 sid). 결정적(key의 해시로 고른다).

    missing=False면 항목 안의 **다른** sid로만 바꾸고, True면 없는 sid(S9)로 바꾼다.
    바꿀 인용이 없거나 다른 sid가 없으면 ValueError.
    """
    found = list(_CITE.finditer(answer))
    if not found:
        raise ValueError("바꿀 [S#] 인용이 없다")
    target = found[_pick(key + ":pos", len(found))]
    old = target.group(1)
    if missing:
        new = MISSING_SID
    else:
        others = [s for s in sids if s != old]
        if not others:
            raise ValueError("항목 안에 바꿀 다른 sid가 없다")
        new = others[_pick(key + ":sid", len(others))]
    swapped = answer[: target.start(1)] + new + answer[target.end(1) :]
    return swapped, old, new


def assemble(items: list[Item]) -> tuple[list[Item], dict[str, Any]]:
    """근거·인용 항목을 dev로 두고 subsplit을 씨앗으로 정한 뒤 중복을 지우고 통계를 낸다."""
    placed = [i.model_copy(update={"split": "dev", "subsplit": subsplit(i.seed_id)}) for i in items]
    kept, removed = dedupe(placed)
    stats: dict[str, Any] = {
        "items": len(kept),
        "removed_duplicates": len(removed),
        "by_kind": dict(Counter(i.kind for i in kept)),
        "by_subsplit": dict(Counter(i.subsplit for i in kept)),
        "by_subtype": dict(Counter(i.subtype or "normal" for i in kept)),
        "seeds_by_subsplit": {
            s: len({i.seed_id for i in kept if i.subsplit == s}) for s in ("calibration", "report")
        },
    }
    return kept, stats


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="guardrail.eval.build_grounding")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("answers", "inject"):
        s = sub.add_parser(name)
        s.add_argument("--estimate", action="store_true")
        s.add_argument("--max-calls", type=int, default=None)
    for name in ("cite", "review", "assemble"):
        sub.add_parser(name).add_argument("--out", default="data/dev")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.cmd in ("answers", "inject") and not args.estimate and args.max_calls is None:
        print("시작하지 않음: --max-calls가 없다(--estimate가 아니면 필수)", file=sys.stderr)
        return 2
    print(f"시작하지 않음: {args.cmd}는 GP1 S1.1에서 구현한다", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
