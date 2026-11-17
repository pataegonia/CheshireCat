"""레드팀 초안을 운영진 gpt-5-nano로 만든다(IN-08, 이슈 #62). 저장소 루트에서
`python -m harness.redteam.draft [--shape 이름 ...] [--only-short] [--round N] [--yes]`.

- 꼴(shapes.SHAPES)마다 초안 수만큼 부른다. 호출 하나에 초안 하나, 기대 범주를 호출에 넘긴다
  (`Job.category`). 프롬프트는 튜터 점검 세트 도구(`tutor.eval.case_drafts.messages_for`)를 그대로
  쓴다: 범주 → action 표, 범주 정의, 꼴 설명, 기대 범주, ID를 지운 KB 섹션 원문만 들어간다.
- 짝인 두 꼴은 같은 KB 섹션을 받는다: 같은 단원에서 고른 두 섹션, 난수 씨앗은 (SEED, 짝, 초안
  번호)로 고정이라 어느 꼴을 골라 부르든 같은 번호의 초안은 같은 섹션이다. 초안 번호는
  `--round N`(기본 0)에서 N × (그 꼴의 초안 수)부터 센다. 다시 돌릴 때는 round를 올려야 다른
  섹션을 받는다(짝은 같은 round에서 같은 섹션).
- 부르기 전에 예상 호출 수를 내고 `--yes`가 없으면 거기서 끝난다. 동시 8, 실패는 한 번 다시
  보내고, 429가 연달아 이어지면 멈춘다(case_drafts와 같은 규칙 · 상수). config/tutor.yaml의
  eval.max_calls를 넘지 않는다.
- 버리는 초안: nano가 붙인 범주가 꼴의 기대 범주와 다른 것(먼저 본다), 꼴의 모양 검사
  (메시지 수 상한 · 마지막 학습자 메시지 최소 글자 수)에 안 맞는 것(그다음) + case_drafts가
  버리는 까닭 전부(형식, 메타, 근거 없음, 공개 문항과 같음, 겹침). 까닭별 수와 꼴별 결과를
  화면에 낸다.
- id는 `int-<꼴 이름>-<세 자리 번호>`(번호는 앞 초안 · 고른 기록 · cases.jsonl 다음부터). 받는 대로
  harness/redteam/draft.jsonl(git 제외)에 덧붙인다. 화면에는 꼴 이름과 숫자만 낸다.
- `--only-short`: cases.jsonl에서 남길 수를 못 채운 꼴만 다시 만든다.
"""

import argparse
import asyncio
import random
import re
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, TextIO

from harness import redteam
from harness.redteam.shapes import SHAPES, Shape
from integration.actions import ActionRule, load_actions
from tutor.eval import case_set
from tutor.eval.case_drafts import (
    CONCURRENCY,
    DEADLINE_S,
    MAX_TOKENS,
    RETRIES,
    TIMEOUT_S,
    Assembler,
    DraftCase,
    Job,
    make_llm,
    messages_for,
)
from tutor.eval.case_set import last_user
from tutor.eval.grader import KB_DIR, score_local
from tutor.eval.run_local import EXIT_RATE_LIMITED, RateLimitStop
from tutor.runtime.config import load_config
from tutor.runtime.llm import Result, TutorLLM, missing_api_env

SEED = 62  # 섹션 고르기 난수 씨앗(이슈 번호). 짝 · 초안 번호와 함께 쓴다
SECTIONS_PER_CALL = 2  # 같은 단원에서 고른 두 섹션
ID_NUMBER = re.compile(r"int-(?P<shape>[a-z_]+)-(?P<n>\d{3})")
WRONG_CATEGORY = "범주 다름"  # nano가 붙인 범주가 꼴의 기대 범주와 다르다
WRONG_SHAPE = "꼴 안 맞음"  # 꼴의 모양 검사(Shape.shape_problem)에 안 맞는다
KEPT = "남음"  # 꼴별 결과에서 남긴 초안


def units(kb: Iterable[str]) -> dict[str, list[str]]:
    """단원(예: FIN-03) → 섹션 ID(정렬)."""
    out: dict[str, list[str]] = {}
    for sid in sorted(kb):
        out.setdefault(sid.rsplit("-", 1)[0], []).append(sid)
    return out


def pair_key(shape: Shape) -> str:
    return "|".join(sorted((shape.name, shape.twin)))


def sections_for(shape: Shape, number: int, kb: Mapping[str, str]) -> tuple[str, ...]:
    """짝 · 초안 번호로 정해지는 두 섹션(같은 단원). 짝인 두 꼴은 같은 번호에서 같은 섹션이다."""
    rng = random.Random(f"{SEED}:{pair_key(shape)}:{number}")  # noqa: S311 - 섹션 고르기
    by_unit = units(kb)
    unit = rng.choice(sorted(u for u, sids in by_unit.items() if len(sids) >= SECTIONS_PER_CALL))
    return tuple(rng.sample(by_unit[unit], SECTIONS_PER_CALL))


def job_for(shape: Shape, sections: tuple[str, ...]) -> Job:
    return Job(shape.axis, shape.form, sections, shape.category)


def plan_jobs(
    shapes: Sequence[Shape], kb: Mapping[str, str], round_: int = 0
) -> list[tuple[Shape, Job]]:
    """꼴마다 초안 수만큼. 섹션을 고르는 초안 번호는 round_ × (그 꼴의 초안 수)부터 센다."""
    return [
        (shape, job_for(shape, sections_for(shape, i, kb)))
        for shape in shapes
        for i in range(round_ * shape.drafts, (round_ + 1) * shape.drafts)
    ]


def last_numbers(ids: Iterable[str]) -> dict[str, int]:
    """꼴 → 지금까지 쓴 가장 큰 번호(int-<꼴>-<번호>)."""
    out: dict[str, int] = {}
    for row_id in ids:
        m = ID_NUMBER.fullmatch(row_id)
        if m and m["shape"] in SHAPES:
            out[m["shape"]] = max(out.get(m["shape"], 0), int(m["n"]))
    return out


def kept_by_shape(cases: Iterable[Mapping[str, Any]]) -> Counter[str]:
    """cases.jsonl의 꼴별 문항 수(꼴은 id에서 읽는다)."""
    return Counter(
        m["shape"] for row in cases if (m := ID_NUMBER.fullmatch(str(row.get("id", ""))))
    )


def short_shapes(cases: Iterable[Mapping[str, Any]]) -> list[Shape]:
    kept = kept_by_shape(cases)
    return [s for s in SHAPES.values() if kept[s.name] < s.keep]


class RedteamAssembler:
    """초안을 받는 대로 행으로 바꾼다. 범주 → 꼴 모양을 먼저 보고, 나머지 버림은
    case_drafts.Assembler가 한다.

    id는 꼴마다 start[꼴] 다음 번호부터 `int-<꼴>-<번호>`로 매긴다. 뺀 초안은 까닭별로
    dropped에, 꼴별 결과(남음 · 버린 까닭)는 by_shape에 센다.
    """

    def __init__(
        self,
        kb: Mapping[str, str],
        table: Mapping[str, ActionRule],
        *,
        start: Mapping[str, int] | None = None,
        seen: Iterable[str] = (),
        public: Iterable[str] = (),
    ) -> None:
        self.inner = Assembler(kb, table, seen=seen, public=public)
        self.number: Counter[str] = Counter(start or {})
        self.by_shape: dict[str, Counter[str]] = {}

    @property
    def dropped(self) -> Counter[str]:
        return self.inner.dropped

    def add(self, shape: Shape, job: Job, case: DraftCase | None) -> dict[str, Any] | None:
        tally = self.by_shape.setdefault(shape.name, Counter())
        reason = None
        if case is not None and case.category != shape.category:
            reason = WRONG_CATEGORY
        elif case is not None and shape.shape_problem([m.model_dump() for m in case.messages]):
            reason = WRONG_SHAPE
        if reason is not None:
            self.dropped[reason] += 1
            tally[reason] += 1
            return None
        before = Counter(self.dropped)
        row = self.inner.add(job, case)
        if row is None:
            tally.update(self.dropped - before)  # Assembler가 센 까닭 하나
            return None
        tally[KEPT] += 1
        self.number[shape.name] += 1
        row["id"] = f"int-{shape.name}-{self.number[shape.name]:03d}"
        row["draft"]["shape"] = shape.name
        return row


async def draft_all(
    llm: TutorLLM,
    jobs: Sequence[tuple[Shape, Job]],
    kb: Mapping[str, str],
    table: Mapping[str, ActionRule],
    on_result: Callable[[Shape, Job, Result], None],
) -> list[Result]:
    """호출마다 결과를 jobs 순서로 돌려준다. 호출 하나가 끝날 때마다 on_result를 부른다."""
    gate = asyncio.Semaphore(CONCURRENCY)
    loop = asyncio.get_running_loop()

    async def one(shape: Shape, job: Job) -> Result:
        async with gate:
            result = await llm.complete(
                "redteam_draft",
                messages_for(job, kb, table),
                DraftCase,
                deadline=loop.time() + DEADLINE_S,
                timeout_s=TIMEOUT_S,
                retries=RETRIES,
            )
        on_result(shape, job, result)
        return result

    return list(await asyncio.gather(*(one(shape, job) for shape, job in jobs)))


def show(
    rows: Sequence[Mapping[str, Any]],
    dropped: Mapping[str, int],
    results: Sequence[Result],
    out: TextIO,
) -> None:
    """꼴별 새 초안 수 · 버린 까닭별 수 · 호출 실패 까닭(숫자만)."""
    made = Counter(row["draft"]["shape"] for row in rows)
    out.write("새 초안(꼴별): " + ", ".join(f"{n} {made[n]}" for n in SHAPES if made[n]) + "\n")
    if dropped:
        out.write("버린 초안: " + ", ".join(f"{k} {n}" for k, n in dropped.items()) + "\n")
    errors = Counter(r.error for r in results if r.error is not None)
    if errors:
        out.write("호출 실패 까닭: " + ", ".join(f"{k} {n}" for k, n in sorted(errors.items())))
        out.write("\n")
    truncated = sum(r.finish_reason == "length" for r in results)
    if truncated:
        out.write(f"출력 상한({MAX_TOKENS})에 잘린 응답 {truncated}개\n")


def show_by_shape(by_shape: Mapping[str, Counter[str]], out: TextIO) -> None:
    """꼴마다 한 줄: "남음 n, <버린 까닭> n, …"(숫자만, SHAPES 순서, 부른 꼴만)."""
    out.write("꼴별 결과:\n")
    for name in SHAPES:
        tally = by_shape.get(name)
        if tally is None:
            continue
        reasons = [(k, n) for k, n in tally.most_common() if k != KEPT]
        parts = [f"{KEPT} {tally[KEPT]}", *(f"{k} {n}" for k, n in reasons)]
        out.write(f"- {name}: {', '.join(parts)}\n")


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m harness.redteam.draft", description="레드팀 초안 만들기(IN-08)"
    )
    ap.add_argument(
        "--shape",
        action="append",
        choices=list(SHAPES),
        metavar="이름",
        help="이 꼴만 만든다(여러 번 쓸 수 있다). 없으면 36개 모두",
    )
    ap.add_argument(
        "--only-short", action="store_true", help="cases.jsonl에서 남길 수를 못 채운 꼴만"
    )
    ap.add_argument(
        "--round",
        type=int,
        default=0,
        metavar="N",
        help="섹션을 고르는 초안 번호를 N × (꼴의 초안 수)부터 센다(기본 0). 다시 돌릴 때 올린다",
    )
    ap.add_argument("--yes", action="store_true", help="예상 호출 수를 확인했고 실제로 부른다")
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    ap = parser()
    args = ap.parse_args(argv)
    try:
        drafts = case_set.read_jsonl(redteam.DRAFT_PATH)
        reviewed = case_set.read_jsonl(redteam.REVIEWED_PATH)
        cases = case_set.read_jsonl(redteam.CASES_PATH)
    except (OSError, ValueError) as exc:
        ap.error(str(exc))
    if args.round < 0:
        ap.error("--round는 0 이상이어야 한다")
    chosen = [SHAPES[n] for n in dict.fromkeys(args.shape or SHAPES)]
    if args.only_short:
        short = {s.name for s in short_shapes(cases)}
        chosen = [s for s in chosen if s.name in short]
    calls = sum(s.drafts for s in chosen)
    worst = calls * (1 + RETRIES)
    cfg = load_config(overrides={"llm.max_completion_tokens": MAX_TOKENS})
    sys.stdout.write(f"nano 예상 호출 {calls}회(꼴 {len(chosen)}개")
    sys.stdout.write(f", 실패하면 한 번씩 다시 보내 최대 {worst}회, 상한 {cfg.eval.max_calls}회)\n")
    if worst > cfg.eval.max_calls:
        ap.error(f"최대 호출 {worst}회가 상한 {cfg.eval.max_calls}회를 넘는다")
    if not chosen:
        sys.stdout.write("만들 꼴이 없다\n")
        return 0
    if not args.yes:
        sys.stdout.write("부르지 않았다. 실제로 부르려면 같은 명령에 --yes를 붙인다\n")
        return 0
    if missing := missing_api_env():
        ap.error(f"운영진 API를 부른다: {'·'.join(missing)}이 필요하다")
    kb = score_local.load_kb_dir(KB_DIR)
    table = load_actions()
    known = [row["id"] for row in [*drafts, *reviewed, *cases]]
    start = last_numbers(known)
    jobs = plan_jobs(chosen, kb, args.round)
    llm = make_llm(cfg)
    llm.max_calls = worst  # 실행 중 멈춤(다시 보내기까지 넣은 수)
    stop = RateLimitStop()
    stop.watch_llm(llm)
    asm = RedteamAssembler(
        kb,
        table,
        start=start,
        seen=[last_user(row.get("messages") or []) for row in [*drafts, *cases]],
        public=[last_user(row["messages"]) for row in case_set.public_rows()],
    )
    rows: list[dict[str, Any]] = []

    def keep(shape: Shape, job: Job, result: Result) -> None:  # 받는 대로 덧붙인다
        parsed = result.parsed if isinstance(result.parsed, DraftCase) else None
        row = asm.add(shape, job, parsed)
        if row is not None:
            case_set.append_rows(redteam.DRAFT_PATH, [row])
            rows.append(row)

    try:
        results = asyncio.run(draft_all(llm, jobs, kb, table, keep))
    except KeyboardInterrupt:
        sys.stdout.write(f"\n멈췄다. 받은 초안 {len(rows)}개는 {redteam.DRAFT_PATH}에 있다\n")
        return 130
    show(rows, asm.dropped, results, sys.stdout)
    show_by_shape(asm.by_shape, sys.stdout)
    sys.stdout.write(f"nano 호출 {sum(llm.calls.values())}회\n")
    if rows:
        sys.stdout.write(f"저장: {redteam.DRAFT_PATH} (새 초안 {len(rows)}개)\n")
        sys.stdout.write("다음: python -m harness.redteam.review\n")
    else:
        sys.stdout.write("남은 초안이 없어 파일에 쓰지 않았다\n")
    if stop.tripped:
        sys.stderr.write(f"{stop.reason()}. 남은 호출은 보내지 않았다\n")
        return EXIT_RATE_LIMITED
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
