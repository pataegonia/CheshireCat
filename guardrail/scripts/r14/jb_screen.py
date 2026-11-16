# ruff: noqa: T201, E501
"""S3.3 탈옥 2차 판정 선별: 오탐 normal 행과 정답 탈옥 행에 check_jailbreak만 반복 호출한다.

1차 판정이 jailbreak_injection이라고 가정하고 2차만 잰다. 호출 = (오탐 행 + 탈옥 행) × repeat.
오탐 행 = 주어진 실행 기록에서 정답 normal인데 한 번이라도 jailbreak_injection으로 본 행.
홀드아웃 행은 쓰지 않는다. 본문은 저장하지 않고 행 id·판정 횟수만 찍는다.

실행(guardrail/에서, 운영진 API 환경변수 필요):
uv run python -X utf8 scripts/r14/jb_screen.py --repeat 3 runs/<a>/responses.jsonl [...]
"""

import argparse
import asyncio
import json
import time
from collections import Counter

from guardrail.config import load_config
from guardrail.data import load_public
from guardrail.eval import cv
from guardrail.judges.input_judge import check_jailbreak
from guardrail.llm import LLM

ap = argparse.ArgumentParser()
ap.add_argument("runs", nargs="+")
ap.add_argument("--repeat", type=int, default=3)
args = ap.parse_args()

cfg = load_config()
split = cv.load_splits()
held = set(split.holdout)
pub = {
    r.id: r
    for r in load_public(cfg.paths.public)
    if r.endpoint == "guard" and split.cluster[r.id] not in held
}
fp = set()
for p in args.runs:
    for line in open(p, encoding="utf-8"):
        rec = json.loads(line)
        r = pub.get(rec["id"])
        if (
            r
            and r.labels["category"] == "normal"
            and rec["response"]["input"]["category"] == "jailbreak_injection"
        ):
            fp.add(r.id)
jb = {i for i, r in pub.items() if r.labels["category"] == "jailbreak_injection"}
ids = sorted(fp) + sorted(jb)


async def main() -> None:
    llm = LLM(cfg.llm)
    await llm.start()
    llm.max_calls = len(ids) * args.repeat * 2  # 재시도 여유
    sem = asyncio.Semaphore(16)

    async def one(i: str) -> tuple[str, str | None]:
        r = pub[i]
        target = next(m.content for m in reversed(r.messages) if m.role == "user")
        async with sem:
            v = await check_jailbreak(
                list(r.messages), target, llm, cfg, deadline=time.monotonic() + cfg.budget_s
            )
        return i, v

    res = await asyncio.gather(*(one(i) for i in ids for _ in range(args.repeat)))
    by: dict[str, Counter] = {i: Counter() for i in ids}
    for i, v in res:
        by[i][v] += 1
    for name, group, good in (
        ("오탐 normal", sorted(fp), "study"),
        ("정답 탈옥", sorted(jb), "apply"),
    ):
        ok = sum(by[i][good] for i in group)
        print(f"{name} {len(group)}행: {good} {ok}/{len(group) * args.repeat}")
        for i in group:
            print(f"  {i} f{split.fold[split.cluster[i]]} {dict(by[i])}")
    print(f"호출 {dict(llm.calls)}")


asyncio.run(main())
