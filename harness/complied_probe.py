"""유해-비하 고정 문장의 응함 판정 탐침(측정 전용).

저장소 루트에서 `python -m harness.complied_probe`.

교차 검증 행 중 정답이 harmful_content/redirect인 행마다, 답변 후보 세 개를 운영진 판정기(complied,
채점기와 같은 프롬프트)에 `--repeat`번씩 보내고 응함 수만 낸다.
- template: 지금 고정 문장(REDIRECT_HARMFUL)
- reference: 그 행의 공개 세트 정답 예시 답변(대조군. 이것도 응함이 비슷하게 나오면 판정 흔들림)
- candidate: 차별 동조를 분명히 거절하는 문구 후보(일반 문장, 과목 사실 없음)
연속 판정 실패가 3번이면 멈춘다. 행 id·후보 이름·수만 출력한다(본문 없음).
"""

import argparse
import sys
from collections import Counter
from collections.abc import Sequence

from harness.oom_probe import cv_rows
from tutor.answer.templates import TEXT, Fixed
from tutor.eval.grader import judge_from_env
from tutor.judge.conversation import tidy
from tutor.judge.flow import _messages
from tutor.request import parse_request
from tutor.retrieval import default_index

CANDIDATE = (
    "특정 지역이나 집단의 사람들을 한꺼번에 깎아내리는 말에는 동의할 수 없어요. "
    "그런 표현은 사용하지 말아 주세요. 수업 내용은 계속 도와줄게요."
)
FAIL_STOP = 3
RESERVED = ("template", "reference")


def top1_titles(rows: Sequence[dict]) -> dict[str, str]:
    """행 → 서버와 같은 검색 질의의 1위 섹션 제목(KB 파생물, API 0회)."""
    index = default_index()
    out = {}
    for row in rows:
        conv = tidy(_messages(parse_request({"messages": row["messages"]})))
        query, history = conv.search_query()
        text = "\n".join([*history, query])
        ranked = index.search(text[-index.max_query_chars :])
        if ranked:
            out[row["id"]] = index.sections[ranked[0][0]].title
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="유해-비하 고정 문장 응함 판정 탐침(운영진 판정기)")
    ap.add_argument("--repeat", type=int, default=4)
    ap.add_argument(
        "--arm",
        action="append",
        default=[],
        metavar="이름=문구",
        help="후보 문구(여러 번). 주면 기본 candidate 대신 잰다. {주제}는 그 행 검색 1위 섹션 제목",
    )
    args = ap.parse_args(argv)
    if not 1 <= args.repeat <= 10:
        ap.error("--repeat는 1~10")
    extra: dict[str, str] = {}
    for spec in args.arm:
        name, sep, text = spec.partition("=")
        if not sep or not name.strip() or not text.strip() or name.strip() in RESERVED:
            ap.error(f"--arm은 이름=문구 형식이다(이름은 {'·'.join(RESERVED)} 말고): {name!r}")
        extra[name.strip()] = text.strip()
    judge = judge_from_env()
    if judge is None:
        ap.error("OPENAI_BASE_URL·OPENAI_MODEL·OPENAI_API_KEY가 필요하다")
    rows = [
        r
        for r in cv_rows()
        if (r["labels"]["category"], r["labels"]["action"]) == ("harmful_content", "redirect")
    ]
    topic = top1_titles(rows) if any("{주제}" in t for t in extra.values()) else {}
    arms = {"template": lambda r: TEXT[Fixed.REDIRECT_HARMFUL]}
    for name, text in (extra or {"candidate": CANDIDATE}).items():
        arms[name] = lambda r, text=text: text.replace("{주제}", topic.get(r["id"], "수업"))
    arms["reference"] = lambda r: r["labels"]["reference_answer"]
    calls = len(rows) * len(arms) * args.repeat
    sys.stdout.write(f"행 {len(rows)}개 × 후보 {len(arms)} × {args.repeat}회 = 판정 {calls}회\n")
    complied: Counter[tuple[str, str]] = Counter()
    errors, streak = Counter(), 0
    for _ in range(args.repeat):
        for row in rows:
            for name, answer in arms.items():
                if streak >= FAIL_STOP:
                    sys.stdout.write("판정 실패가 이어져 멈췄다\n")
                    return 1
                try:
                    out = judge("complied", {"messages": row["messages"], "answer": answer(row)})
                except Exception:  # 운영진 JudgeError 등, 메시지는 남기지 않는다
                    errors[name] += 1
                    streak += 1
                    continue
                streak = 0
                complied[(name, row["id"])] += bool(out["complied"])
    for name in arms:
        total = sum(complied[(name, r["id"])] for r in rows)
        per_row = " ".join(f"{r['id']} {complied[(name, r['id'])]}" for r in rows)
        sys.stdout.write(
            f"- {name}: 응함 {total}/{len(rows) * args.repeat - errors[name]}"
            f" (판정 실패 {errors[name]}) | {per_row}\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
