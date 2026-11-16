"""run_local 여러 번을 차례로 돌린다(측정 전용, 런박스 무인 실행).

`python -m tutor.eval.run_queue <plan.yaml> --summary <파일> --max-calls N`

계획 파일(items 목록)의 항목마다 `run_local --preset tutor`를 그 workdir에서 부른다. 시작 전에
항목마다 `--max-calls 0`으로 run_local을 불러(입력 검사·예상 호출 수만 내고 API를 부르기 전에
멈춘다) 예상 호출을 읽어 출력하고, 합이 --max-calls를 넘거나 읽지 못한 항목이 있으면 아무것도
돌리지 않는다. 실제 실행은 항목마다 자기 예상 호출을 --max-calls로 넘겨 합이 상한 안에 든다.
종료 코드가 0이 아닌 항목(기본값이 섞인 tainted 1 포함)에서 멈춘다. 항목이 끝날 때마다 요약
파일에 이름·명령·점수 줄(접두어로 고른 줄만)·결과 JSON 경로를 덧붙인다. 응답 본문은 run_local도
내지 않고, 이 도구는 고른 줄만 옮긴다.
"""

import argparse
import os
import re
import subprocess  # noqa: S404  run_local을 workdir에서 돌린다(셸 없이 인자 목록으로)
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, TextIO

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

RUN_LOCAL = ("-m", "tutor.eval.run_local")
ESTIMATE = re.compile(r"운영진 API 예상 호출 (\d+)회")
EMBEDDING = re.compile(r"임베딩 예상 호출 (\d+)회")
SAVED = "저장: "
CAP_REFUSAL = "상한 0회를 넘는다"  # --max-calls 0 예상 실행이 정상으로 멈출 때 run_local 오류 문구
EXIT_REFUSED = 2
# 요약 파일에 옮기는 run_local 출력 줄(접두어). 점수·항목별·범주별·지연·비교·튜터 실패 줄만
SUMMARY_PREFIXES = (
    "공개 부분 세트 ",
    "운영진 API 예상 호출",
    "튜터 점수(실행별)",
    "교차 검증 행 튜터 점수",
    "항목별 평균",
    "교차 검증 행 항목별(실행별)",
    "- action ",
    "- citation ",
    "- facts ",
    "- faithful ",
    "- 응함 ",
    "범주별 점수",
    "튜터 요청 지연",
    "튜터 LLM 최종 실패",
    "상한에 막혀",
    "속도 제한(429)",
    "새로 틀린 문항",
    "이전 결과에만 있는 행",
)


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: Annotated[str, Field(min_length=1)]
    workdir: str = "."
    cases: Annotated[str, Field(min_length=1)]
    runs: PositiveInt
    judge: bool
    tutor_set: list[str] = []
    compare: str | None = None


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    items: Annotated[list[Item], Field(min_length=1)]

    @model_validator(mode="after")
    def _unique_names(self) -> "Plan":
        names = [item.name for item in self.items]
        if len(set(names)) != len(names):
            raise ValueError("항목 이름이 겹친다")
        return self


@dataclass(frozen=True)
class Done:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], Path], Done]


def child_env(cwd: Path) -> dict[str, str]:
    """자식 환경: 출력 UTF-8, 그리고 가드도 workdir 것을 먼저 읽는다(tutor는 -m이 cwd에서 읽는다).

    가드는 대기열 가상환경에 저장소 사본으로 설치돼 있어, 다른 worktree를 workdir로 주면 그대로는
    대기열 쪽 가드 코드가 섞인다.
    """
    env = {**os.environ, "PYTHONUTF8": "1"}
    guard_src = cwd.resolve() / "guardrail" / "src"
    if guard_src.is_dir():
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(guard_src), env.get("PYTHONPATH")]))
    return env


def subprocess_runner(args: Sequence[str], cwd: Path) -> Done:
    """workdir에서 `python -m tutor.eval.run_local <args>`를 부르고 출력을 모은다."""
    env = child_env(cwd)
    proc = subprocess.run(  # noqa: S603  인자 목록, 셸 없음, 이 파이썬으로 run_local만 부른다
        [sys.executable, *RUN_LOCAL, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return Done(proc.returncode, proc.stdout, proc.stderr)


def load_plan(path: Path) -> Plan:
    return Plan.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def item_args(item: Item) -> list[str]:
    args = ["--preset", "tutor", "--cases", item.cases, "--runs", str(item.runs)]
    args += ["--judge"] if item.judge else []
    for override in item.tutor_set:
        args += ["--tutor-set", override]
    args += ["--compare", item.compare] if item.compare else []
    return args


def _last_line(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else "(출력 없음)"


def estimate(item: Item, run: Runner) -> int:
    """`--max-calls 0`으로 부른 run_local이 낸 예상 호출(운영진 API + 임베딩). 못 읽으면 ValueError.

    run_local은 입력을 모두 검사하고 예상 호출을 낸 뒤, 상한 0을 넘어 API를 부르기 전에 멈춘다.
    """
    done = run([*item_args(item), "--max-calls", "0"], Path(item.workdir))
    calls = ESTIMATE.search(done.stdout)
    # 예상 줄 뒤의 다른 검사(임베딩 환경변수 등)에서 멈췄으면 실제 실행도 멈춘다: 상한 거절만 받는다
    capped = CAP_REFUSAL in _last_line(done.stderr)
    if done.returncode != EXIT_REFUSED or calls is None or not capped:
        raise ValueError(f"{item.name}: 예상 호출을 읽지 못했다: {_last_line(done.stderr)}")
    embedding = EMBEDDING.search(done.stdout)
    return int(calls.group(1)) + (int(embedding.group(1)) if embedding else 0)


def summary_lines(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if line.startswith(SUMMARY_PREFIXES)]


def result_path(stdout: str) -> str | None:
    saved = [line[len(SAVED) :] for line in stdout.splitlines() if line.startswith(SAVED)]
    return saved[-1] if saved else None


def block(item: Item, args: Sequence[str], done: Done) -> str:
    """요약 파일 한 항목: 이름·시각·종료 코드, 명령, 고른 출력 줄, 결과 JSON 경로."""
    lines = [f"## {item.name} ({_now()}, 종료 코드 {done.returncode})"]
    lines.append(f"run_local {' '.join(args)} (workdir {item.workdir})")
    lines += summary_lines(done.stdout)
    if done.returncode != 0:
        lines.append(f"stderr 마지막 줄: {_last_line(done.stderr)}")
    lines.append(f"결과 JSON: {result_path(done.stdout) or '없음'}")
    return "\n".join(lines) + "\n\n"


def _append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_queue(
    plan: Plan,
    summary: Path,
    max_calls: int,
    run: Runner = subprocess_runner,
    out: TextIO | None = None,
) -> int:
    """계획을 차례로 돈다. 시작 전 거절은 EXIT_REFUSED, 실패한 항목에서 멈추면 그 종료 코드."""
    out = sys.stdout if out is None else out
    try:
        planned = [(item, estimate(item, run)) for item in plan.items]
    except ValueError as exc:
        out.write(f"시작하지 않는다: {exc}\n")
        return EXIT_REFUSED
    for item, calls in planned:
        out.write(f"- {item.name}: 예상 호출 {calls}회\n")
    total = sum(calls for _item, calls in planned)
    out.write(f"예상 호출 합 {total}회(상한 {max_calls}회)\n")
    if total > max_calls:
        out.write("상한을 넘어 시작하지 않는다\n")
        return EXIT_REFUSED
    _append(summary, f"# run_queue 시작 {_now()}, 항목 {len(planned)}개, 예상 호출 {total}회\n\n")
    for number, (item, calls) in enumerate(planned, 1):
        out.write(f"[{number}/{len(planned)}] {item.name} 시작\n")
        args = [*item_args(item), "--max-calls", str(calls)]
        done = run(args, Path(item.workdir))
        text = block(item, args, done)
        _append(summary, text)
        out.write(text)
        if done.returncode != 0:
            out.write(f"{item.name}이 종료 코드 {done.returncode}로 끝나 멈춘다\n")
            return done.returncode
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="run_local 여러 번을 차례로(측정 전용)")
    ap.add_argument("plan", type=Path, help="계획 YAML(items 목록)")
    ap.add_argument("--summary", type=Path, required=True, help="항목별 요약을 덧붙일 파일")
    ap.add_argument("--max-calls", type=int, required=True, help="모든 항목 예상 호출 합 상한")
    args = ap.parse_args(argv)
    try:
        plan = load_plan(args.plan)
    except (OSError, ValueError, yaml.YAMLError) as exc:  # pydantic ValidationError도 ValueError
        ap.error(f"계획 파일을 읽지 못했다: {exc}")
    return run_queue(plan, args.summary, args.max_calls)


if __name__ == "__main__":
    raise SystemExit(main())
