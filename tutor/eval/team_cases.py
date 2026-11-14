"""팀 작성 점검 세트 도구(TU-16·TU-19, #80). 런박스 저장소 루트에서 부른다.

- `python -m tutor.eval.team_cases generate`: nano 예상 호출 수만 낸다. `--yes`를 붙이면 운영진
  gpt-5-nano로 초안을 만들어 team_cases/draft.jsonl(git 제외)에 덧붙인다(case_drafts)
- `python -m tutor.eval.team_cases review`: 초안을 읽고 골라 team_cases/tutor.jsonl에 남긴다
  (case_review)
- `python -m tutor.eval.team_cases check [FILE...]`: 점검 세트 파일을 검사하고 축·action·범주별
  문항 수를 낸다(파일이 없으면 team_cases/tutor.jsonl)
- `python -m tutor.eval.team_cases show`: team_cases/tutor.jsonl을 검사한 뒤 문항을 줄마다 JSON
  하나로 내고, 마지막 줄에 축·action별 수를 낸다(런박스에서 복사해 개발 PC로 옮길 때)
측정은 `python -m tutor.eval.run_local --preset tutor --cases team`. 서버는 이 도구를 읽지 않는다.
"""

import argparse
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from tutor.eval import case_drafts, case_review, case_set
from tutor.eval.case_set import AXES, MIN_PER_AXIS
from tutor.eval.grader import KB_DIR, score_local


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


def _load(path: Path, kb: Mapping[str, str]) -> list[dict[str, Any]] | None:
    """검사한 문항. 읽지 못하거나 틀리면 까닭(본문 없이)을 내고 None."""
    try:
        return case_set.load_cases(path, kb)
    except OSError as exc:
        sys.stdout.write(f"{path.name}: 읽지 못했다({type(exc).__name__})\n")
    except ValueError as exc:
        sys.stdout.write(f"{exc}\n")
    return None


def _check(_ap: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    kb = score_local.load_kb_dir(KB_DIR)
    ok = True
    for path in args.files or [case_set.FINAL_PATH]:
        cases = _load(path, kb)
        if cases is None:
            ok = False
        else:
            _show_counts(path, cases, sys.stdout)
    return 0 if ok else 1


def _show(_ap: argparse.ArgumentParser, _args: argparse.Namespace) -> int:
    """남긴 문항(줄마다 JSON 하나)과 마지막 한 줄의 축·action별 수. 그 밖의 것은 내지 않는다."""
    path = case_set.FINAL_PATH
    cases = _load(path, score_local.load_kb_dir(KB_DIR))
    if cases is None:
        return 1
    sys.stdout.writelines(json.dumps(row, ensure_ascii=False) + "\n" for row in cases)
    axes = Counter(row["axis"] for row in cases)
    actions = Counter(row["labels"]["action"] for row in cases)
    sys.stdout.write(
        f"{path.name} 문항 {len(cases)}개 · 축 "
        + ", ".join(f"{axis} {axes[axis]}" for axis in AXES)
        + " · action "
        + ", ".join(f"{a} {n}" for a, n in sorted(actions.items()))
        + "\n"
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m tutor.eval.team_cases", description="팀 작성 점검 세트(TU-16·TU-19)"
    )
    sub = ap.add_subparsers(dest="command", required=True)
    commands = (
        ("generate", "운영진 nano로 초안 만들기(--yes 없이는 예상 호출 수만)", case_drafts),
        ("review", "초안을 읽고 골라 점검 세트에 남기기", case_review),
    )
    for name, text, module in commands:
        p = sub.add_parser(name, help=text, description=text)
        module.configure(p)
        p.set_defaults(run=module.run, parser=p)
    check = sub.add_parser("check", help="점검 세트 파일 검사·축별 문항 수")
    check.add_argument("files", nargs="*", type=Path, help="없으면 team_cases/tutor.jsonl")
    check.set_defaults(run=_check, parser=check)
    show = sub.add_parser("show", help="남긴 문항(tutor.jsonl)과 축·action별 수를 화면에")
    show.set_defaults(run=_show, parser=show)
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return args.run(args.parser, args)


if __name__ == "__main__":
    raise SystemExit(main())
