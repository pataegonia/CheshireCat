"""튜터 세부 지표(TU-16)와 로컬 측정(T1.5).

채점은 운영진 로컬 채점기 함수를 그대로 쓰고, 튜터 점수는 로컬 채점기와 0.005 안에서 같아야 한다.
판정은 가짜 판정기로만 부른다(운영진 API를 부르지 않는다).
"""

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tutor.eval import grader, metrics, run_local
from tutor.eval.grader import KB_DIR, score_local
from tutor.runtime.response import TUTOR_DEFAULT

VERDICTS = {"facts": {"score": 0.5}, "faithful": {"ok": True}, "complied": {"complied": False}}
ROOT = Path(__file__).resolve().parents[2]
# 서버 경로만 새 프로세스에서 돌리고, 측정 코드·운영진 채점기가 메모리에 올라왔는지 본다
SERVER_ONLY = """
import asyncio, sys, types
import tutor
state = types.SimpleNamespace()
asyncio.run(tutor.startup(state))
assert state.tutor_kb is not None  # 기동 훅이 실제로 돌았다
body ={"spec_version": "1.0", "session_id": "s", "messages": [{"role": "user", "content": "x"}]}
asyncio.run(tutor.handle(body))
grader = {"judge", "scoring", "score_local"}
print(" ".join(sorted(m for m in sys.modules if m.startswith("tutor.eval") or m in grader)))
"""


@pytest.fixture(scope="module")
def kb():
    return score_local.load_kb_dir(KB_DIR)


@pytest.fixture(scope="module")
def rows():
    return run_local.public_tutor_rows()


def fake_judge(kind, _payload):
    return VERDICTS[kind]


def failing_faithful(kind, payload):
    if kind == "faithful":
        raise RuntimeError("판정 실패")
    return fake_judge(kind, payload)


def counting(calls):
    def judge(kind, payload):
        calls.append(kind)
        return fake_judge(kind, payload)

    return judge


def row(gold, *, category="normal", row_id="r", evidence=()):
    labels = {"action": gold, "category": category, "evidence": list(evidence), "gold_facts": []}
    return {"id": row_id, "endpoint": "tutor", "messages": [], "labels": labels}


def scored(gold, pred, *, category="normal", row_id="r", default=False):
    """정답 action과 낸 응답만으로 채점한 문항 하나(판정 없음, 근거 없음)."""
    resp = dict(TUTOR_DEFAULT) if default else {"answer": "a", "action": pred, "citations": []}
    return metrics.score_one(row(gold, category=category, row_id=row_id), resp, kb={})


def local_grader(rows, responses, judge, kb):
    ordered = iter([responses[row["id"]] for row in rows])
    return score_local.score_local(rows, lambda _e, _p: next(ordered), judge, kb, workers=1)


@pytest.mark.parametrize("preset", ["tutor", "reference", "reference_top1"])
def test_tutor_score_matches_the_local_grader(rows, kb, preset):
    responses = run_local.responses_for(preset, rows)
    report = metrics.build_report([metrics.score_run(rows, responses, kb=kb)])
    theirs = local_grader(rows, responses, None, kb)
    assert abs(report.tutor[0] - theirs["tutor"]) <= 0.005  # #71 수용 기준
    assert report.by_category == theirs["per_category"]["tutor"]


@pytest.mark.parametrize("judge", [fake_judge, failing_faithful], ids=["judged", "judge_fails"])
def test_judged_scores_match_the_local_grader(rows, kb, judge):
    responses = run_local.responses_for("reference", rows)
    report = metrics.build_report([metrics.score_run(rows, responses, judge=judge, kb=kb)])
    theirs = local_grader(rows, responses, judge, kb)
    assert abs(report.tutor[0] - theirs["tutor"]) <= 0.005
    assert report.by_category == theirs["per_category"]["tutor"]
    assert report.judge_errors == (theirs["judge_errors"],)


def test_presets_are_ordered(rows, kb):
    tutor_scores = {
        preset: metrics.build_report(
            [metrics.score_run(rows, run_local.responses_for(preset, rows), kb=kb)]
        ).tutor[0]
        for preset in ("tutor", "reference_top1", "reference")
    }
    assert tutor_scores["reference"] == 100.0  # 판정 없이는 action·인용만 채점된다
    assert tutor_scores["tutor"] < tutor_scores["reference_top1"] < tutor_scores["reference"]


def test_confusion_false_blocks_and_categories():
    run = [
        scored("answer", "answer", category="normal", row_id="a"),
        scored("answer", "refuse", category="normal", row_id="b"),
        scored("scaffold", "redirect", category="answer_seeking", row_id="c"),
        scored("refuse", "refuse", category="jailbreak_injection", row_id="d"),
        scored("clarify", "refuse", category="low_signal_input", row_id="e"),
    ]
    report = metrics.build_report([run])
    assert report.confusion["answer"] == {"answer": 1, "refuse": 1}
    assert report.false_blocks == (2,)  # 정답이 answer·scaffold인데 refuse·redirect
    assert report.by_category == {
        "answer_seeking": 0.0,
        "jailbreak_injection": 100.0,
        "low_signal_input": 0.0,
        "normal": 50.0,
    }
    assert report.components["action"] == 0.4


def test_invalid_responses_count_as_invalid_action():
    run = [metrics.score_one(row("answer", row_id="x"), None, kb={})]
    report = metrics.build_report([run])
    assert report.confusion == {"answer": {"invalid": 1}}
    assert report.tutor == (0.0,)


def test_stable_perfect_flaky_and_regressions():
    run1 = [scored("answer", "answer", row_id="a"), scored("answer", "answer", row_id="b")]
    run2 = [scored("answer", "answer", row_id="a"), scored("answer", "refuse", row_id="b")]
    report = metrics.build_report([run1, run2])
    assert report.runs == 2 and report.tutor == (100.0, 50.0)
    assert report.stable_perfect == 0.5
    assert report.flaky == ("b",)
    assert metrics.regressions({"a": [1.0], "b": [1.0]}, report) == ["b"]
    assert metrics.regressions({"a": [0.0], "b": [0.5]}, report) == []


def test_judge_error_rows_have_no_points():
    resp = {"answer": "a", "action": "answer", "citations": []}
    failed = metrics.score_one(row("answer", row_id="x"), resp, judge=failing_faithful, kb={})
    judged = metrics.score_one(row("answer", row_id="x"), resp, judge=fake_judge, kb={})
    report = metrics.build_report(
        [
            [failed, scored("answer", "answer", row_id="y")],
            [judged, scored("answer", "answer", row_id="y")],
        ]
    )
    assert failed.points is None and report.judge_errors == (1, 0)
    assert report.tutor == (100.0, 100.0)  # 상한 안의 판정 오류는 채점기처럼 뺀다
    assert report.points == {"x": (None, 1.0), "y": (1.0, 1.0)}
    assert report.stable_perfect == 0.5  # 판정 오류가 난 행은 만점으로 치지 않는다
    assert report.flaky == ()  # 판정 오류는 흔들림으로 보지 않는다
    assert report.by_category == {"normal": 100.0}
    assert metrics.regressions({"x": [1.0], "y": [1.0]}, report) == []
    assert metrics.regressions({"x": [None, 1.0]}, report) == []  # 전에도 만점이 아니었다


def test_missed_evidence_lists_rows_without_every_gold_section():
    def cite(row_id, citations):
        resp = {"answer": "a", "action": "answer", "citations": citations}
        return metrics.score_one(row("answer", row_id=row_id, evidence=["A", "B"]), resp, kb={})

    invalid = metrics.score_one(row("answer", row_id="bad", evidence=["A"]), None, kb={})
    no_evidence = scored("answer", "answer", row_id="free")
    run = [cite("half", ["A"]), cite("full", ["B", "A"]), cite("none", []), invalid, no_evidence]
    assert metrics.build_report([run]).missed_evidence == ("bad", "half", "none")


def test_default_path_gold_actions():
    run = [
        scored("clarify", "clarify", row_id="a", default=True),
        scored("answer", "clarify", row_id="b", default=True),
        scored("answer", "answer", row_id="c"),
    ]
    assert metrics.build_report([run]).default_gold == {"answer": 1, "clarify": 1}


def test_build_report_needs_runs():
    with pytest.raises(ValueError):
        metrics.build_report([])


def test_score_run_cancels_queued_rows_when_interrupted():
    calls = []

    def interrupting(kind, payload):
        calls.append(kind)
        if payload["answer"] == "stop":
            raise KeyboardInterrupt
        time.sleep(0.05)
        return fake_judge(kind, payload)

    batch = [row("answer", row_id=f"r{i}") for i in range(64)]  # 행마다 충실 판정 1회
    responses = {
        r["id"]: {"answer": "stop" if r["id"] == "r0" else "a", "action": "answer", "citations": []}
        for r in batch
    }
    with pytest.raises(KeyboardInterrupt):
        metrics.score_run(batch, responses, judge=interrupting, kb={})
    assert len(calls) < 32  # 줄 서 있던 행은 판정을 부르지 않는다(다 돌면 64회)


def test_unknown_preset_is_rejected(rows):
    with pytest.raises(ValueError):
        run_local.responses_for("nope", rows)


def test_failed_calls_count_as_missing_responses(monkeypatch, rows, capsys):
    async def broken(_body):
        raise RuntimeError("본문이 섞일 수 있는 메시지")

    monkeypatch.setattr(run_local.tutor, "handle", broken)
    assert run_local.responses_for("tutor", rows[:3]) == {row["id"]: None for row in rows[:3]}
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "메시지" not in err  # 예외 종류만 낸다


def test_expected_calls_follow_the_grader_rules(rows):
    # 공개 튜터 200행: 사실 119 + 충실 147 + 응함 44 = 실행당 판정 310회, 재시도 여유 1.1배
    assert run_local.expected_calls(rows, preset="reference", runs=3, judge=True) == 1023
    assert run_local.expected_calls(rows, preset="reference", runs=3, judge=False) == 0
    assert run_local.expected_calls(rows, preset="tutor", runs=1, judge=False) == 660


def test_judge_from_env_needs_all_three_settings(monkeypatch):
    for name in ("OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_KEY"):
        monkeypatch.setenv(name, "x")
    assert callable(grader.judge_from_env())  # 만들기만 하고 부르지 않는다
    monkeypatch.delenv("OPENAI_API_KEY")
    assert grader.judge_from_env() is None


def test_cli_writes_a_report_and_compares(tmp_path, capsys):
    assert run_local.main(["--preset", "reference", "--out", str(tmp_path)]) == 0
    saved = next(tmp_path.glob("reference-*.json"))
    data = json.loads(saved.read_text(encoding="utf-8"))
    assert data["preset"] == "reference" and data["judge"] is False
    assert data["report"]["tutor"] == [100.0]
    argv = ["--preset", "tutor", "--out", str(tmp_path), "--compare", str(saved)]
    assert run_local.main(argv) == 0
    out = capsys.readouterr().out
    assert "튜터 점수" in out and "새로 틀린 문항" in out and "오차단" in out
    assert "예상 호출" in out


def test_cli_compare_needs_points_and_the_same_judge_mode(tmp_path):
    old = tmp_path / "old.json"
    argv = ["--preset", "reference", "--out", str(tmp_path), "--compare", str(old)]
    for content in (
        {"judge": False, "report": {"tutor": [1.0]}},
        {"judge": True, "report": {"points": {}}},
    ):
        old.write_text(json.dumps(content), encoding="utf-8")
        with pytest.raises(SystemExit):
            run_local.main(argv)
    assert not list(tmp_path.glob("reference-*.json"))  # 재기 전에 멈춘다


def test_cli_reads_responses_from_a_file(tmp_path, rows, capsys):
    responses = run_local.responses_for("reference", rows)
    picked = [row["id"] for row in rows[:5]]
    path = tmp_path / "harness.jsonl"
    lines = [json.dumps({"id": i, "response": responses[i]}, ensure_ascii=False) for i in picked]
    lines += ["", json.dumps({"id": "not-public", "response": None})]  # 빈 줄은 건너뛴다
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert run_local.load_responses(path) == {
        **{i: responses[i] for i in picked},
        "not-public": None,
    }
    assert run_local.main(["--responses", str(path), "--out", str(tmp_path)]) == 0
    assert "공개 튜터 행 5개" in capsys.readouterr().out
    data = json.loads(next(tmp_path.glob("harness-*.json")).read_text(encoding="utf-8"))
    assert data["report"]["rows"] == 5 and data["report"]["tutor"] == [100.0]  # 파일에 있는 행만


@pytest.mark.parametrize(
    "text",
    [
        '{"id": "a"}\n',
        "not json\n",
        '{"id": 1, "response": null}\n',
        '{"id": "a", "response": 1}\n' * 2,
    ],
    ids=["missing_response", "not_json", "id_not_string", "duplicate_id"],
)
def test_bad_response_files_are_rejected(tmp_path, text):
    path = tmp_path / "bad.jsonl"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        run_local.load_responses(path)
    with pytest.raises(SystemExit):
        run_local.main(["--responses", str(path), "--out", str(tmp_path)])


def test_cli_rejects_a_file_without_public_ids(tmp_path):
    path = tmp_path / "other.jsonl"
    path.write_text(json.dumps({"id": "nope", "response": None}) + "\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        run_local.main(["--responses", str(path), "--out", str(tmp_path)])


def test_cli_budget_counts_every_judge_call(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(run_local, "judge_from_env", lambda: counting(calls))
    argv = ["--preset", "reference", "--judge", "--out", str(tmp_path), "--max-calls"]
    with pytest.raises(SystemExit):
        run_local.main([*argv, "340"])  # 예상 341회(310 × 1.1)가 상한을 넘으면
    assert calls == [] and not list(tmp_path.iterdir())  # 판정기를 한 번도 부르지 않는다
    assert run_local.main([*argv, "341"]) == 0
    assert len(calls) == 310  # 실제 판정 수는 예상 안이다
    data = json.loads(next(tmp_path.glob("reference-*.json")).read_text(encoding="utf-8"))
    assert data["judge"] is True and data["report"]["components"]["facts"] == 0.5


def test_cli_stops_before_calling_the_tutor_when_over_budget(monkeypatch, tmp_path):
    called = []

    async def handle(_body):
        called.append(1)
        return dict(TUTOR_DEFAULT)

    monkeypatch.setattr(run_local.tutor, "handle", handle)
    with pytest.raises(SystemExit):
        run_local.main(["--preset", "tutor", "--max-calls", "100", "--out", str(tmp_path)])
    assert called == []


def test_cli_collects_every_run_in_one_event_loop(monkeypatch, tmp_path):
    loops = []

    async def handle(_body):
        loops.append(asyncio.get_running_loop())
        return dict(TUTOR_DEFAULT)

    monkeypatch.setattr(run_local.tutor, "handle", handle)
    assert run_local.main(["--preset", "tutor", "--runs", "2", "--out", str(tmp_path)]) == 0
    assert len(loops) == 400 and all(loop is loops[0] for loop in loops)


def test_cli_never_prints_or_saves_bodies(tmp_path, capsys, rows):
    for preset in ("reference", "reference_top1", "tutor"):
        assert run_local.main(["--preset", preset, "--out", str(tmp_path)]) == 0
    saved = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("*.json"))
    shown = capsys.readouterr().out + saved
    bodies = [row["labels"]["reference_answer"] for row in rows] + [TUTOR_DEFAULT["answer"]]
    bodies += [m.get("content") or "" for row in rows for m in row["messages"]]
    assert [b for b in bodies if len(b) >= 8 and b in shown] == []


def test_server_does_not_load_measurement_code():
    # 시연 때 서버 코드를 확인한다(SPEC §8). 측정 코드는 공개 세트 정답을 읽으니
    # 서버와 섞이지 않게 한다
    argv = [sys.executable, "-c", SERVER_ONLY]
    done = subprocess.run(argv, capture_output=True, text=True, check=True, cwd=ROOT, timeout=120)  # noqa: S603
    assert done.stdout.strip() == ""


def test_cli_judge_needs_the_api_settings(monkeypatch, tmp_path):
    for name in ("OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit):
        run_local.main(["--preset", "reference", "--judge", "--out", str(tmp_path)])


def test_cli_rejects_zero_runs(tmp_path):
    with pytest.raises(SystemExit):
        run_local.main(["--preset", "reference", "--runs", "0", "--out", str(tmp_path)])
