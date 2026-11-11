"""팀 작성 점검 세트(TU-16·TU-19, #80). 문항은 tutor/eval/team_cases/*.jsonl에 둔다.

- 줄마다 공개 세트 튜터 행과 같은 꼴에 축(axis)을 더한다. 채점은 공개 세트와 같은 채점기로 한다
- 팀이 직접 쓰거나 운영진 gpt-5-nano로 만든 문항만 둔다(다른 LLM 금지). 서버는 읽지 않는다
- `python -m tutor.eval.team_cases`: 축과, 공개 세트가 근거로 쓰지 않은 KB 섹션을 낸다
- `python -m tutor.eval.team_cases FILE...`: 파일을 검사하고 축·action·범주별 문항 수를 낸다
측정은 `python -m tutor.eval.run_local --preset tutor --cases FILE`. 출력에 문항 본문은 없다.
"""

import argparse
import json
import sys
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from tutor.eval.grader import KB_DIR, PUBLIC_DIR, score_local, scoring

AXES = {  # TU-19 축(이름 → 내용)
    "unseen_section": "공개 세트에 근거로 나오지 않은 섹션을 묻는 정상·scaffold·abstain 질문",
    "paraphrase": "같은 의도의 표현 변형(존댓말·반말·오타·영어·혼합 언어·띄어쓰기 우회)",
    "mixed": "섞인 발화(정상 질문 + 탈옥·남의 정보, 위기 + 학습 질문, 두 섹션 비교)",
    "structure": "구조 변형(버튼 문구만 있는 1턴, 1턴 저신호, 10턴 넘는 대화, 긴 코드 붙여넣기)",
    "premise": "전제 오류 질문 대 자료에 없는 질문(answer 대 abstain 경계)",
}
MIN_PER_AXIS = 10  # TU-19 각 축 최소 10개
FIELDS = frozenset({"id", "endpoint", "axis", "messages", "labels"})
LABELS = frozenset({"action", "category", "evidence"})  # action·응함(범주)만, 근거는 고를 때만
ROLES = ("user", "assistant")


def _messages_ok(messages: Any) -> bool:
    """SPEC §3 요청 꼴: user·assistant 메시지 목록이고 첫·마지막은 user다."""
    if not isinstance(messages, list) or not messages:
        return False
    for m in messages:
        if not isinstance(m, dict) or m.get("role") not in ROLES:
            return False
        if not isinstance(m.get("content"), str):
            return False
    return messages[0]["role"] == messages[-1]["role"] == "user"


def _unknown(keys: Iterable[str], known: Collection[str], prefix: str = "") -> str | None:
    extra = sorted(set(keys) - set(known))
    return f"모르는 필드 {', '.join(prefix + k for k in extra)}" if extra else None


def problem(row: Any, kb_ids: Collection[str]) -> str | None:
    """행에서 틀린 필드 이름(문항 본문은 넣지 않는다). 맞으면 None."""
    if not isinstance(row, dict):
        return "행 전체"
    if unknown := _unknown(row, FIELDS):
        return unknown
    if not isinstance(row.get("id"), str) or not row["id"]:
        return "id"
    if row.get("endpoint") != "tutor":
        return "endpoint"
    if not isinstance(row.get("axis"), str) or row["axis"] not in AXES:
        return "axis"
    if not _messages_ok(row.get("messages")):
        return "messages"
    labels = row.get("labels")
    if not isinstance(labels, dict):
        return "labels"
    if unknown := _unknown(labels, LABELS, "labels."):
        return unknown
    if labels.get("action") not in scoring.ACTIONS:
        return "labels.action"
    if labels.get("category") not in scoring.CATEGORIES:
        return "labels.category"
    evidence = labels.get("evidence", [])
    if not isinstance(evidence, list) or not all(
        isinstance(sid, str) and sid in kb_ids for sid in evidence
    ):
        return "labels.evidence"
    return None


def load_cases(path: Path, kb_ids: Collection[str]) -> list[dict[str, Any]]:
    """점검 세트 파일을 읽고 검사한다. 틀린 줄이 있으면 줄 번호와 필드를 모두 모아 ValueError.

    BOM은 건너뛰고 줄은 \\n으로만 나눈다(문자열 안의 U+2028 등은 줄 끝이 아니다).
    """
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").split("\n"), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            errors.append(f"{number}번째 줄: JSON이 아니다")
            continue
        field = problem(row, kb_ids)
        if field is None and row["id"] in seen:
            field = "id(앞 줄과 겹침)"
        if field is not None:
            errors.append(f"{number}번째 줄: {field}")
            continue
        seen.add(row["id"])
        rows.append(row)
    if errors:
        raise ValueError(f"{path.name} 형식 오류 {len(errors)}곳: " + "; ".join(errors))
    if not rows:
        raise ValueError(f"{path.name}: 문항이 없다")
    return rows


def uncovered_sections(rows: Iterable[Mapping[str, Any]], kb_ids: Collection[str]) -> list[str]:
    """공개 세트가 한 번도 근거로 쓰지 않은 KB 섹션(PRD §1: 48개 중 7개)."""
    cited = {sid for row in rows for sid in row["labels"].get("evidence") or []}
    return sorted(set(kb_ids) - cited)


def _title(sid: str, text: str) -> str:
    heading = next((line.lstrip("#").strip() for line in text.splitlines() if line[:1] == "#"), "")
    return heading.removeprefix(sid).strip()


def _show_guide(kb: Mapping[str, str], out: TextIO) -> None:
    out.write(f"점검 세트 축(TU-19, 축마다 최소 {MIN_PER_AXIS}개):\n")
    for axis, text in AXES.items():
        out.write(f"- {axis}: {text}\n")
    public = score_local._read_jsonl(PUBLIC_DIR / "tutor.jsonl")
    unseen = uncovered_sections(public, kb)
    out.write(f"공개 세트가 근거로 쓰지 않은 KB 섹션 {len(unseen)}개:\n")
    for sid in unseen:
        out.write(f"- {sid} {_title(sid, kb[sid])}\n")


def _show_counts(path: Path, cases: Sequence[Mapping[str, Any]], out: TextIO) -> None:
    axes = Counter(row["axis"] for row in cases)
    actions = Counter(row["labels"]["action"] for row in cases)
    categories = Counter(row["labels"]["category"] for row in cases)
    out.write(f"{path.name}: 문항 {len(cases)}개\n")
    out.write("축: " + ", ".join(f"{axis} {axes[axis]}" for axis in AXES) + "\n")
    out.write("action: " + ", ".join(f"{a} {n}" for a, n in sorted(actions.items())) + "\n")
    out.write("범주: " + ", ".join(f"{c} {n}" for c, n in sorted(categories.items())) + "\n")
    thin = [f"{axis} {axes[axis]}" for axis in AXES if axes[axis] < MIN_PER_AXIS]
    if thin:
        out.write(f"최소 {MIN_PER_AXIS}개가 안 되는 축: {', '.join(thin)}\n")


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="팀 작성 점검 세트 안내·검사(TU-19)")
    ap.add_argument("files", nargs="*", type=Path, help="점검 세트 JSONL. 없으면 안내만 낸다")
    args = ap.parse_args(argv)
    kb = score_local.load_kb_dir(KB_DIR)
    if not args.files:
        _show_guide(kb, sys.stdout)
        return 0
    ok = True
    for path in args.files:
        try:
            cases = load_cases(path, kb)
        except OSError as exc:
            sys.stdout.write(f"{path.name}: 읽지 못했다({type(exc).__name__})\n")
            ok = False
        except ValueError as exc:
            sys.stdout.write(f"{exc}\n")
            ok = False
        else:
            _show_counts(path, cases, sys.stdout)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
