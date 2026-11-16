"""run_local 대기열(run_queue, 측정 전용): 가짜 실행기로 예상 호출·상한·순서·멈춤·요약을 본다."""

from pathlib import Path

import pytest
import yaml

from tutor.eval import run_queue as rq

PLAN_DIR = Path(rq.__file__).resolve().parent / "plans"

STDOUT = """홀드아웃 0행은 재지 않는다(통합 하네스에서 최대 3회, TU-16·#84)
공개 부분 세트 target_set.jsonl: 공개 튜터 행 46개를 잰다
운영진 API 예상 호출 {calls}회(재시도 여유 1.1배, 상한 {calls}회)
튜터 점수(실행별): 80.00, 81.00, 범위 80.00~81.00 (문항 46개, 실행 2회, 판정 오류 0)
교차 검증 행 튜터 점수(실행별, 설정 비교용): 80.00, 81.00 (범위 80.00~81.00), 문항 46개
항목별 평균: action 0.900, citation 0.800, facts 0.700, faithful 0.950, 응함 0
교차 검증 행 항목별(실행별):
- action 0.900, 0.900 (범위 0.900~0.900)
- citation 0.800, 0.800 (범위 0.800~0.800)
- facts 0.700, 0.700 (범위 0.700~0.700)
- faithful 0.950, 0.950 (범위 0.950~0.950)
- 응함 0, 0 (범위 0~0)
교차 검증 행에서 응함으로 잡힌 문항(실행별):
- 1회 0개
범주별 점수: normal 85.0, out_of_material 70.0
action 혼동행렬(정답 → 낸 것):
- answer: answer 58, clarify 2
튜터 LLM 호출: main 92회
튜터 요청 지연(초, 동시 8): p50 5.00 · p95 9.00 · 최대 12.00 (요청 92개)
저장: /runs/{name}.json
"""


def item(name, **kw):
    return {"name": name, "cases": "t.jsonl", "runs": 2, "judge": True, **kw}


def plan(*items):
    return rq.Plan.model_validate({"items": list(items)})


class FakeRunner:
    """`--max-calls 0`(예상)이면 run_local처럼 예상 줄을 내고 2로 끝난다. 실제 실행은 codes대로."""

    def __init__(self, calls=100, codes=None, estimate_ok=True):
        self.calls, self.codes, self.estimate_ok = calls, codes or {}, estimate_ok
        self.log: list[tuple[list[str], Path]] = []

    def __call__(self, args, cwd):
        args = list(args)
        self.log.append((args, cwd))
        name = args[args.index("--cases") + 1]
        if args[-2:] == ["--max-calls", "0"]:
            if not self.estimate_ok:
                return rq.Done(2, "", "run_local.py: error: 튜터 설정 파일을 읽지 못했다\n")
            out = f"운영진 API 예상 호출 {self.calls}회(재시도 여유 1.1배, 상한 0회)\n"
            return rq.Done(2, out, f"error: 예상 호출 {self.calls}회가 상한 0회를 넘는다\n")
        code = self.codes.get(name, 0)
        return rq.Done(code, STDOUT.format(calls=self.calls, name=name), "pub-1: TimeoutError\n")

    def real_runs(self):
        return [args for args, _cwd in self.log if args[-2:] != ["--max-calls", "0"]]


def test_estimate_comes_first_and_over_the_cap_runs_nothing(tmp_path, capsys):
    runner = FakeRunner(calls=400)
    summary = tmp_path / "summary.md"
    p = plan(item("a"), item("b"))
    assert rq.run_queue(p, summary, 799, run=runner) == rq.EXIT_REFUSED
    out = capsys.readouterr().out
    assert out == (
        "- a: 예상 호출 400회\n- b: 예상 호출 400회\n"
        "예상 호출 합 800회(상한 799회)\n상한을 넘어 시작하지 않는다\n"
    )
    assert runner.real_runs() == [] and not summary.exists()


def test_items_run_in_order_with_their_own_cap(tmp_path, capsys):
    runner = FakeRunner(calls=300)
    summary = tmp_path / "out" / "summary.md"
    p = plan(
        item("a", cases="a.jsonl", judge=False),
        item("b", cases="b.jsonl", workdir="../wt", tutor_set=['prompt.main_call="x.md"'],
             compare="runs/base.json"),
    )  # fmt: skip
    assert rq.run_queue(p, summary, 600, run=runner) == 0
    first, second = runner.real_runs()
    assert first == ["--preset", "tutor", "--cases", "a.jsonl", "--runs", "2", "--max-calls", "300"]
    assert second == [
        "--preset", "tutor", "--cases", "b.jsonl", "--runs", "2", "--judge",
        "--tutor-set", 'prompt.main_call="x.md"', "--compare", "runs/base.json",
        "--max-calls", "300",
    ]  # fmt: skip
    assert [cwd for _a, cwd in runner.log] == [Path("."), Path("../wt"), Path("."), Path("../wt")]
    text = summary.read_text(encoding="utf-8")
    assert text.startswith("# run_queue 시작 ") and "항목 2개, 예상 호출 600회" in text
    assert text.count("\n## ") == 2 and "종료 코드 0" in text
    assert "결과 JSON: /runs/a.jsonl.json\n" in text and "결과 JSON: /runs/b.jsonl.json\n" in text
    out = capsys.readouterr().out
    assert "[1/2] a 시작\n" in out and "[2/2] b 시작\n" in out


def test_summary_keeps_only_score_lines(tmp_path):
    runner = FakeRunner()
    summary = tmp_path / "s.md"
    assert rq.run_queue(plan(item("a")), summary, 100, run=runner) == 0
    block = summary.read_text(encoding="utf-8").split("\n## ", 1)[1]
    kept = block.strip().splitlines()[2:-1]  # 머리 줄·명령 줄과 결과 줄 사이
    assert kept == [
        "공개 부분 세트 target_set.jsonl: 공개 튜터 행 46개를 잰다",
        "운영진 API 예상 호출 100회(재시도 여유 1.1배, 상한 100회)",
        "튜터 점수(실행별): 80.00, 81.00, 범위 80.00~81.00 (문항 46개, 실행 2회, 판정 오류 0)",
        "교차 검증 행 튜터 점수(실행별, 설정 비교용): 80.00, 81.00 (범위 80.00~81.00), 문항 46개",
        "항목별 평균: action 0.900, citation 0.800, facts 0.700, faithful 0.950, 응함 0",
        "교차 검증 행 항목별(실행별):",
        "- action 0.900, 0.900 (범위 0.900~0.900)",
        "- citation 0.800, 0.800 (범위 0.800~0.800)",
        "- facts 0.700, 0.700 (범위 0.700~0.700)",
        "- faithful 0.950, 0.950 (범위 0.950~0.950)",
        "- 응함 0, 0 (범위 0~0)",
        "범주별 점수: normal 85.0, out_of_material 70.0",
        "튜터 요청 지연(초, 동시 8): p50 5.00 · p95 9.00 · 최대 12.00 (요청 92개)",
    ]
    assert "혼동행렬" not in block  # 고른 줄만 옮긴다
    assert "TimeoutError" not in block  # 성공한 항목은 stderr를 옮기지 않는다


def test_queue_stops_at_the_first_failing_item(tmp_path, capsys):
    runner = FakeRunner(codes={"b.jsonl": 1})
    summary = tmp_path / "s.md"
    p = plan(item("a", cases="a.jsonl"), item("b", cases="b.jsonl"), item("c", cases="c.jsonl"))
    assert rq.run_queue(p, summary, 1000, run=runner) == 1
    assert [args[3] for args in runner.real_runs()] == ["a.jsonl", "b.jsonl"]  # c는 돌지 않는다
    text = summary.read_text(encoding="utf-8")
    assert text.count("\n## ") == 2 and "## b (" in text and "종료 코드 1)" in text
    assert "stderr 마지막 줄: pub-1: TimeoutError" in text
    assert "b이 종료 코드 1로 끝나 멈춘다" in capsys.readouterr().out


def test_keep_going_tainted_continues_but_still_stops_on_other_failures(tmp_path, capsys):
    runner = FakeRunner(codes={"b.jsonl": rq.EXIT_TAINTED, "c.jsonl": 3})
    summary = tmp_path / "s.md"
    items = [item(n, cases=f"{n}.jsonl") for n in ("a", "b", "c", "d")]
    code = rq.run_queue(plan(*items), summary, 1000, run=runner, keep_going_tainted=True)
    assert code == 3
    assert [args[3] for args in runner.real_runs()] == ["a.jsonl", "b.jsonl", "c.jsonl"]
    assert "b은 tainted(기본값 섞임)로 끝났다" in capsys.readouterr().out


def test_unreadable_estimate_refuses_to_start(tmp_path, capsys):
    runner = FakeRunner(estimate_ok=False)
    summary = tmp_path / "s.md"
    assert rq.run_queue(plan(item("a")), summary, 1000, run=runner) == rq.EXIT_REFUSED
    assert "시작하지 않는다: a: 예상 호출을 읽지 못했다" in capsys.readouterr().out
    assert runner.real_runs() == [] and not summary.exists()


def test_embedding_estimate_is_added():
    def runner(args, cwd):
        out = "운영진 API 예상 호출 100회(…)\n임베딩 예상 호출 7회(…)\n"
        return rq.Done(2, out, "run_local.py: error: 예상 호출 107회가 상한 0회를 넘는다\n")

    assert rq.estimate(rq.Item(**item("a")), runner) == 107


def test_a_check_failing_after_the_estimate_line_refuses_to_start():
    # 예상 줄을 낸 뒤 다른 검사(임베딩 환경변수 등)에서 멈춘 항목은 실제 실행에서도 멈춘다
    def runner(args, cwd):
        out = "운영진 API 예상 호출 100회(…)\n"
        return rq.Done(2, out, "run_local.py: error: 임베딩을 켜면 EMBEDDING_API_KEY이 필요하다\n")

    with pytest.raises(ValueError, match="임베딩을 켜면"):
        rq.estimate(rq.Item(**item("a")), runner)


@pytest.mark.parametrize(
    "bad",
    [
        {"items": []},
        {"items": [item("a"), item("a")]},
        {"items": [{**item("a"), "typo": 1}]},
        {"items": [{**item("a"), "runs": 0}]},
        {"items": [{**item("a"), "judge": "yes"}]},
        {"items": [{k: v for k, v in item("a").items() if k != "cases"}]},
    ],
    ids=["empty", "same_name", "unknown_key", "zero_runs", "loose_bool", "no_cases"],
)
def test_bad_plans_fail_before_anything_runs(tmp_path, bad, capsys):
    path = tmp_path / "plan.yaml"
    path.write_text(yaml.safe_dump(bad, allow_unicode=True), encoding="utf-8")
    with pytest.raises(SystemExit):
        rq.main([str(path), "--summary", str(tmp_path / "s.md"), "--max-calls", "10"])
    assert "계획 파일을 읽지 못했다" in capsys.readouterr().err
    assert not (tmp_path / "s.md").exists()


def test_child_reads_the_guard_of_its_workdir(tmp_path, monkeypatch):
    # 다른 worktree를 workdir로 주면 가드도 그 worktree 것을 먼저 읽는다(PYTHONPATH 맨 앞)
    monkeypatch.setenv("PYTHONPATH", "keep")
    (tmp_path / "wt" / "guardrail" / "src").mkdir(parents=True)
    env = rq.child_env(tmp_path / "wt")
    first, rest = env["PYTHONPATH"].split(rq.os.pathsep)
    assert Path(first) == (tmp_path / "wt" / "guardrail" / "src").resolve() and rest == "keep"
    assert env["PYTHONUTF8"] == "1"
    assert rq.child_env(tmp_path)["PYTHONPATH"] == "keep"  # 가드 폴더가 없으면 그대로


def test_example_plan_is_three_judged_baseline_runs_on_the_target_set():
    p = rq.load_plan(PLAN_DIR / "target_baseline.yaml")
    (only,) = p.items
    assert (only.workdir, only.runs, only.judge, only.tutor_set, only.compare) == (
        ".", 3, True, [], None,
    )  # fmt: skip
    assert only.cases == "tutor/eval/runs/target_set.jsonl"
