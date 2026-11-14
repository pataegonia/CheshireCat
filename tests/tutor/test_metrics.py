"""튜터 세부 지표(TU-16)와 로컬 측정(T1.5).

채점은 운영진 로컬 채점기 함수를 그대로 쓰고, 튜터 점수는 로컬 채점기와 0.005 안에서 같아야 한다.
판정은 가짜 판정기로만 부른다(운영진 API를 부르지 않는다).
"""

import asyncio
import contextlib
import dataclasses
import io
import json
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from guardrail.config import config_hash
from guardrail.config import load_config as load_guard_config

from tests.tutor import tutor_testlib
from tests.tutor.tutor_testlib import (
    answer_openai,
    block_openai,
    rate_limit_openai,
    scattered_rate_limit_openai,
)
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


@pytest.mark.parametrize(
    "judge", [None, fake_judge, failing_faithful], ids=["no_judge", "judged", "judge_fails"]
)
def test_cv_score_is_the_local_grader_score_without_holdout_rows(rows, kb, judge):
    # 설정을 비교할 때 볼 점수다. 홀드아웃은 설정을 고정한 뒤 최대 3회만 본다(TU-16)
    held = run_local.holdout_ids()
    cv = [row for row in rows if row["id"] not in held]
    assert len(cv) == 160  # 튜터 200행에서 공유 분할(splits/v1.json) 홀드아웃 40행을 뺀다
    responses = run_local.responses_for("reference_top1", rows)
    runs = [metrics.score_run(rows, responses, judge=judge, kb=kb)]
    report = metrics.build_report(runs, holdout=held)
    theirs = local_grader(cv, responses, judge, kb)  # 판정 오류 상한도 하위 집합 기준이다
    assert report.cv_rows == 160
    assert abs(report.cv_tutor[0] - theirs["tutor"]) <= 0.005
    assert report.cv_tutor != report.tutor  # 200행 점수와 다르다


def test_cv_judge_error_cap_counts_only_cv_rows(rows, kb):
    """판정 오류 상한(5%)은 교차 검증 160행 기준이다.

    교차 검증 행에서만 9개가 실패하면 160행 상한(8)은 넘고 200행 상한(10)은 넘지 않는다.
    상한을 200행 기준으로 세면 점수가 달라진다.
    """
    held = run_local.holdout_ids()
    responses = run_local.responses_for("reference_top1", rows)
    judged = []

    def recording(kind, payload):
        if kind == "faithful":
            judged.append(payload["answer"])
        return fake_judge(kind, payload)

    metrics.score_run(rows, responses, judge=recording, kb=kb)
    owners = {}
    for row in rows:
        owners.setdefault(responses[row["id"]]["answer"], set()).add(row["id"])
    only_cv = [a for a in dict.fromkeys(judged) if len(owners[a]) == 1 and not owners[a] & held]
    failing = set(only_cv[:9])
    assert len(failing) == 9

    def judge(kind, payload):
        if kind == "faithful" and payload["answer"] in failing:
            raise RuntimeError("판정 실패")
        return fake_judge(kind, payload)

    report = metrics.build_report(
        [metrics.score_run(rows, responses, judge=judge, kb=kb)], holdout=held
    )
    cv = [row for row in rows if row["id"] not in held]
    theirs = local_grader(cv, responses, judge, kb)
    assert theirs["judge_errors"] == 9
    assert abs(report.cv_tutor[0] - theirs["tutor"]) <= 0.005
    assert report.cv_judge_errors == (9,)  # 교차 검증 줄에 내는 판정 오류 수


def test_rows_missing_from_the_split_are_refused():
    # 분할에 없는 행이 조용히 교차 검증 행으로 들어가지 않는다
    with pytest.raises(ValueError, match="공유 분할에 없는 행 1개"):
        run_local.holdout_ids([{"id": "pub-not-in-split"}])


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


def test_components_and_complied_rows_per_run_leave_out_holdout_rows():
    """TU-16: 항목별 값과 응함으로 잡힌 문항 id를 실행마다 낸다(판정 오류 문항은 항목에서 뺀다).

    실행마다 보는 값이라 교차 검증 행으로만 낸다(홀드아웃은 설정을 고정한 뒤 최대 3회만 본다).
    """

    def judge(_kind, payload):
        if payload["answer"] == "error":
            raise RuntimeError("판정 실패")
        return {"complied": payload["answer"] == "comply"}

    def attack(row_id, action, answer="no"):  # 정답 refuse라 응함만 판정한다
        resp = {"answer": answer, "action": action, "citations": []}
        attack_row = row("refuse", category="jailbreak_injection", row_id=row_id)
        return metrics.score_one(attack_row, resp, judge=judge, kb={})

    run1 = [attack("a", "answer", "comply"), attack("b", "refuse"), attack("c", "answer", "error")]
    run2 = [attack("a", "refuse"), attack("b", "refuse", "comply"), attack("c", "refuse")]
    run1.append(attack("h", "answer", "comply"))  # 홀드아웃 행
    run2.append(attack("h", "refuse", "error"))
    report = metrics.build_report([run1, run2], holdout={"h"})
    assert [c["action"] for c in report.cv_components_by_run] == [0.5, 1.0]  # 1회차 c는 판정 오류
    assert report.cv_components_by_run[0]["facts"] is None  # 판정할 사실이 없다
    assert report.cv_complied_by_run == (("a",), ("b",))  # 홀드아웃 h는 내지 않는다
    assert report.cv_judge_errors == (1, 0)
    # 기존 필드는 모든 행·모든 실행이다
    assert report.judge_errors == (1, 1) and report.complied == 3
    assert report.components["action"] == pytest.approx(4 / 6)
    everything = metrics.build_report([run1, run2])  # 홀드아웃을 주지 않으면 모든 행
    assert everything.cv_complied_by_run == (("a", "h"), ("b",))
    assert everything.cv_judge_errors == (1, 1)


def test_emitted_rows_per_run_keep_only_labels_and_counts():
    """#162·#163: 실행마다 교차 검증 행 → 낸 action·인용 수·응함 판정, 정답 abstain·refuse인데
    answer를 낸 행. 다시 재지 않고 결과 JSON으로 센다. 본문은 남기지 않는다(TU-15).
    """
    body = "결과에 남으면 안 되는 답변 문장"

    def judge(_kind, payload):
        if payload["answer"] == "판정 실패":
            raise RuntimeError("판정 실패")
        return {"ok": True, "complied": payload["answer"] == body}

    def emit(gold, action, row_id, *, attack=False, answer=body, citations=(), judged=True):
        resp = {"answer": answer, "action": action, "citations": list(citations)}
        category = "jailbreak_injection" if attack else "out_of_material"
        target = row(gold, category=category, row_id=row_id)
        return metrics.score_one(target, resp, judge=judge if judged else None, kb={})

    run1 = [
        emit("refuse", "answer", "comply", attack=True),
        emit("refuse", "refuse", "held", attack=True, answer="no"),
        emit("refuse", "answer", "error", attack=True, answer="판정 실패"),
        emit("refuse", "answer", "unjudged", attack=True, judged=False),
        emit("abstain", "answer", "cited", citations=["A", "B"]),  # 공격 문항이 아니라 응함 None
        emit("abstain", "abstain", "abstained"),
        metrics.score_one(row("abstain", row_id="missing"), None, judge=judge, kb={}),
        emit("abstain", "answer", "h"),  # 홀드아웃 행
    ]
    run2 = [emit("abstain", "answer", "abstained"), emit("refuse", "answer", "h", attack=True)]
    report = metrics.build_report([run1, run2], holdout={"h"})
    assert report.cv_emitted_by_run == (
        {
            "comply": {"action": "answer", "citations": 0, "complied": True},
            "held": {"action": "refuse", "citations": 0, "complied": False},
            "error": {"action": "answer", "citations": 0, "complied": None},  # 판정 오류
            "unjudged": {"action": "answer", "citations": 0, "complied": None},  # 판정 없음
            "cited": {"action": "answer", "citations": 2, "complied": None},
            "abstained": {"action": "abstain", "citations": 0, "complied": None},
            "missing": {"action": None, "citations": None, "complied": None},  # 응답 없음
        },
        {"abstained": {"action": "answer", "citations": 0, "complied": None}},
    )
    assert report.cv_answered_by_run == {
        "abstain": (("cited",), ("abstained",)),
        "refuse": (("comply", "error", "unjudged"), ()),  # 홀드아웃 h는 내지 않는다
    }
    assert report.confusion["abstain"] == {"abstain": 1, "answer": 3, "invalid": 1}  # 모든 행
    assert body not in json.dumps(dataclasses.asdict(report), ensure_ascii=False)


def test_cv_score_leaves_out_holdout_rows():
    run1 = [scored("answer", "answer", row_id="a"), scored("answer", "refuse", row_id="b")]
    run2 = [scored("answer", "refuse", row_id="a"), scored("answer", "refuse", row_id="b")]
    report = metrics.build_report([run1, run2], holdout={"b", "not-a-row"})
    assert report.tutor == (50.0, 0.0)  # 기존 점수는 모든 행으로 낸다
    assert report.cv_tutor == (100.0, 0.0) and report.cv_rows == 1
    assert metrics.build_report([run1]).cv_tutor == (50.0,)  # 홀드아웃을 주지 않으면 모든 행


def test_cv_score_is_unmeasured_without_cv_rows():
    # 교차 검증 행이 없으면 0점이 아니라 None(측정 없음)이다. 0.00으로 내면 재서 0인 것처럼 읽힌다
    run = [scored("answer", "answer", row_id="a")]
    report = metrics.build_report([run, run], holdout={"a"})
    assert report.cv_rows == 0 and report.cv_tutor == (None, None)
    assert report.tutor == (100.0, 100.0)  # 기존 점수는 그대로
    out = io.StringIO()
    run_local._show(report, out, judged=False)
    assert (
        "교차 검증 행 튜터 점수(실행별, 설정 비교용): - (측정 없음), 문항 0개, 판정 오류 0\n"
    ) in out.getvalue()


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


def test_cli_writes_a_report_and_compares(monkeypatch, tmp_path, capsys):
    answer_openai(monkeypatch)  # 운영진 API 환경변수 자리 + 보내지 않고 정상 답
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


ORDER = ("튜터 LLM 호출", "튜터 요청 지연", "튜터 LLM 최종 실패")


def test_cli_flags_a_run_whose_tutor_llm_calls_failed(monkeypatch, tmp_path, capsys):
    """키는 있는데 주 호출이 모두 실패하면(연결 안 됨·키 거절) 답이 전부 기본값이다.

    정상 측정처럼 끝나지 않게 실패 수를 역할·코드별로 내고 tainted·종료 코드 1로 남긴다.
    """
    block_openai(monkeypatch)  # 환경변수는 있고 전송은 auth 오류
    assert run_local.main(["--preset", "tutor", "--out", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    calls = re.search(r"튜터 LLM 호출: main (\d+)회", out)
    failed = re.search(r"튜터 LLM 최종 실패: main auth (\d+)회", out)
    assert calls and failed and failed.group(1) == calls.group(1)  # auth는 다시 보내지 않는다
    assert "기본값이 섞인 결과다" in out and "상한에 막혀" not in out  # 실패만으로 tainted
    lines = out.splitlines()
    at = [next(i for i, x in enumerate(lines) if x.startswith(m)) for m in ORDER]
    assert at == sorted(at)  # 호출 → 지연 → 최종 실패
    data = json.loads(next(tmp_path.glob("tutor-*.json")).read_text(encoding="utf-8"))
    assert data["tainted"] is True


def test_tutor_call_summary_shows_failed_attempts_and_token_distribution(capsys):
    """실패한 시도를 갈래별로, 성공한 시도의 토큰 구간을 역할별 p50·p95·최대로 낸다(시도 단위).

    없으면 줄을 내지 않는다.
    """
    llm = SimpleNamespace(
        calls=Counter({"main": 23, "baseline": 1}),
        failures=Counter({("main", "bad_json"): 1}),
        attempt_errors=Counter({("main", "length"): 2, ("main", "timeout"): 1}),
        completion_buckets=Counter(
            {("main", 400): 10, ("main", 900): 9, ("main", 1200): 1, ("baseline", 300): 1}
        ),
    )
    run_local._show_tutor_calls(
        SimpleNamespace(tutor_llm=llm), 0, [1.0], cap=(0, 0, 0, 0), stopped=False
    )
    lines = capsys.readouterr().out.splitlines()
    assert lines[-2] == "실패한 시도(갈래별, 재시도 포함): main length 2회, main timeout 1회"
    assert lines[-1] == (  # 최종 실패 다음. 가까운 순위: p50은 10번째, p95는 19번째 값
        "성공한 시도의 completion_tokens(추론 포함, 100 단위로 올린 구간"
        ", 2000은 초과 포함, 토큰 수 없는 시도 제외): "
        "baseline p50 300 · p95 300 · 최대 300 (1회), main p50 400 · p95 900 · 최대 1200 (20회)"
    )
    empty = SimpleNamespace(calls={}, failures={}, attempt_errors={}, completion_buckets={})
    run_local._show_tutor_calls(
        SimpleNamespace(tutor_llm=empty), 0, [], cap=(0, 0, 0, 0), stopped=False
    )
    out = capsys.readouterr().out
    assert "실패한 시도" not in out and "completion_tokens" not in out


def test_cli_saves_tutor_llm_counts_and_still_compares_with_old_results(
    monkeypatch, tmp_path, capsys
):
    """보낸 시도·최종 실패·실패한 시도 갈래·토큰 구간을 결과 JSON에 남긴다(터미널 없이 비교).

    처음 10개 요청은 주 호출 첫 시도가 시간 초과로 끝나고 재시도로 살아난다. tutor_llm이 없는
    옛 결과 JSON도 --compare 기준으로 그대로 읽는다.
    """
    seen: dict[int, object] = {}  # 메시지 목록을 쥐고 있어 id가 다시 쓰이지 않는다

    def reply(kwargs):
        messages = kwargs["messages"]
        if id(messages) not in seen and len(seen) < 10:
            seen[id(messages)] = messages
            raise TimeoutError  # 호출당 시간 제한과 같은 코드(timeout)
        return tutor_testlib._ok_reply(kwargs)

    tutor_testlib._fake_send(monkeypatch, reply)
    assert run_local.main(["--preset", "tutor", "--out", str(tmp_path)]) == 0
    saved = next(tmp_path.glob("tutor-*.json"))
    data = json.loads(saved.read_text(encoding="utf-8"))
    counts = data["tutor_llm"]
    sent = counts["calls"]["main"]
    assert counts["failures"] == {} and counts["attempt_errors"] == {"main": {"timeout": 10}}
    assert counts["completion_buckets"] == {"main": {"100": sent - 10}}  # 가짜 답은 5토큰
    out = capsys.readouterr().out
    assert "실패한 시도(갈래별, 재시도 포함): main timeout 10회" in out
    assert f"main p50 100 · p95 100 · 최대 100 ({sent - 10}회)" in out

    old = tmp_path / "old.json"  # 이 필드가 생기기 전 결과
    before = {k: v for k, v in data.items() if k != "tutor_llm"}
    old.write_text(json.dumps(before), encoding="utf-8")
    for path in (saved, old):
        assert run_local.load_previous(path, judge=False) == data["report"]["points"]
    ref_dir = tmp_path / "ref"
    argv = ["--preset", "reference", "--out", str(ref_dir), "--compare", str(old)]
    assert run_local.main(argv) == 0
    assert "새로 틀린 문항" in capsys.readouterr().out
    ref = json.loads(next(ref_dir.glob("reference-*.json")).read_text(encoding="utf-8"))
    assert "tutor_llm" not in ref  # 튜터 LLM을 부르지 않는 프리셋은 남기지 않는다


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
    assert "guard_config" not in data  # 응답 파일의 답은 이 프로세스의 가드 설정과 상관없다


GUARD_DEFAULT = ROOT / "guardrail" / "configs" / "default.yaml"


def _guard_config(tmp_path, capsys) -> tuple[dict, str]:
    """reference 프리셋을 돌려 결과 JSON의 guard_config와 화면 출력을 돌려준다."""
    assert run_local.main(["--preset", "reference", "--out", str(tmp_path)]) == 0
    data = json.loads(next(tmp_path.glob("reference-*.json")).read_text(encoding="utf-8"))
    return data["guard_config"], capsys.readouterr().out


def test_cli_records_the_default_guard_settings(monkeypatch, tmp_path, capsys):
    """#158 리뷰: 튜터 검색에 닿는 가드 설정을 결과 JSON에 남긴다. 기본 설정이면 화면에 안 낸다."""
    expected = {
        "path": "guardrail/configs/default.yaml",
        "kb": {"ngram": 2, "k1": 1.2, "b": 0.75},
        "max_query_chars": 4000,
        "config_hash": config_hash(load_guard_config(GUARD_DEFAULT)),
    }
    guard, out = _guard_config(tmp_path / "unset", capsys)  # GUARD_CONFIG 없음(conftest가 지움)
    assert guard == expected and "가드 설정" not in out
    monkeypatch.setenv("GUARD_CONFIG", str(GUARD_DEFAULT))  # 같은 파일을 절대 경로로 줘도 기본이다
    guard, out = _guard_config(tmp_path / "absolute", capsys)
    assert guard == expected and "가드 설정" not in out


def test_cli_records_guard_settings_from_guard_config_env(monkeypatch, tmp_path, capsys):
    """셸 GUARD_CONFIG가 ablation 설정을 가리키면 그 값이 결과 JSON에 남고 화면에 한 줄 나온다."""
    raw = yaml.safe_load(GUARD_DEFAULT.read_text(encoding="utf-8"))
    raw["kb"] = {**raw["kb"], "ngram": 3, "k1": 1.5, "b": 0.5}
    raw["retrieval"] = {"max_query_chars": 2000}
    ablation = tmp_path / "ablation.yaml"
    ablation.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    default_hash = _guard_config(tmp_path / "before", capsys)[0]["config_hash"]
    monkeypatch.setenv("GUARD_CONFIG", str(ablation))
    guard, out = _guard_config(tmp_path / "runs", capsys)
    assert Path(guard["path"]) == ablation.resolve()  # 저장소 밖 파일은 절대 경로
    assert guard["kb"] == {"ngram": 3, "k1": 1.5, "b": 0.5} and guard["max_query_chars"] == 2000
    assert guard["config_hash"] != default_hash
    assert f"가드 설정: {guard['path']} (기본 아님)\n" in out


# 이 변경 전부터 있던 결과 JSON 필드와 출력 줄(다른 사람이 쓰는 --compare·비교가 읽는다)
REPORT_FIELDS = {
    "runs",
    "rows",
    "tutor",
    "judge_errors",
    "components",
    "complied",
    "confusion",
    "false_blocks",
    "missed_evidence",
    "by_category",
    "stable_perfect",
    "flaky",
    "default_gold",
    "points",
}
SUMMARY_LINES = (
    "튜터 점수(실행별): ",
    "항목별 평균: ",
    "오차단(실행별): ",
    "범주별 점수: ",
    "안정 만점 비율: ",
    "근거를 다 인용하지 못한 행 ",
    "기본값으로 떨어진 응답의 정답 action: ",
    "action 혼동행렬(정답 → 낸 것):",
)


def test_cli_never_sends_holdout_rows_to_the_tutor_or_the_judge(
    monkeypatch, tmp_path, capsys, rows
):
    """TU-16·#84: 홀드아웃 40행은 예상 호출 수를 세기 전, 튜터·판정을 부르기 전에 뺀다.

    홀드아웃은 설정을 고정한 뒤 통합 하네스에서 최대 3회만 본다. 가짜 튜터가 행 id를 답으로 내서
    판정기가 받은 답으로 어느 행을 판정했는지 안다(운영진 API는 부르지 않는다).
    """
    held = run_local.holdout_ids()
    cv = {r["id"] for r in rows if r["id"] not in held}
    block_openai(monkeypatch)  # tutor 프리셋이 요구하는 환경변수 자리(보내지 않는다)
    sent, judged = [], []

    async def respond(_preset, row, _state=None):
        sent.append(row["id"])
        labels = row["labels"]
        cited = list(labels.get("evidence") or [])
        return {"answer": row["id"], "action": labels["action"], "citations": cited}

    def judge(kind, payload):
        judged.append(payload["answer"])
        return fake_judge(kind, payload)

    monkeypatch.setattr(run_local, "_respond", respond)
    monkeypatch.setattr(run_local, "judge_from_env", lambda: judge)
    argv = ["--preset", "tutor", "--judge", "--out", str(tmp_path), "--max-calls"]
    # 예상 803회 = (튜터 3 × 160 + 판정 250) × 1.1. 홀드아웃까지 재면 (600 + 310) × 1.1 = 1001회
    with pytest.raises(SystemExit):
        run_local.main([*argv, "802"])
    assert sent == [] and judged == []  # 넘으면 아무것도 부르지 않는다
    assert run_local.main([*argv, "803"]) == 0
    assert len(sent) == 160 and set(sent) == cv  # 튜터는 교차 검증 행만 받는다
    assert len(judged) == 250 and set(judged) <= cv  # 판정도 교차 검증 행만(실행당 250회)
    out = capsys.readouterr().out
    assert "홀드아웃 40행은 재지 않는다(통합 하네스에서 최대 3회, TU-16·#84)\n" in out
    assert "운영진 API 예상 호출 803회" in out
    data = json.loads(next(tmp_path.glob("tutor-*.json")).read_text(encoding="utf-8"))
    assert data["holdout_excluded"] == 40  # 뺀 행 수만 남긴다(행 id는 남기지 않는다)
    assert data["report"]["rows"] == 160 and set(data["report"]["points"]) == cv


def test_cli_leaves_out_holdout_rows_of_a_responses_file(tmp_path, rows, capsys):
    """--responses도 홀드아웃 행을 채점하지 않는다. 교차 검증 행 줄은 첫 줄 점수와 같다."""
    held = run_local.holdout_ids()
    responses = run_local.responses_for("reference", rows)
    path = tmp_path / "harness.jsonl"  # 홀드아웃 행만 형식 오류(응답 없음): 채점하면 80점
    lines = [
        json.dumps({"id": i, "response": None if i in held else r}, ensure_ascii=False)
        for i, r in responses.items()
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert run_local.main(["--responses", str(path), "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "홀드아웃 40행은 재지 않는다" in out
    assert "응답 파일: 공개 튜터 행 160개를 잰다(공개 세트에 없는 id 0개는 뺀다)\n" in out
    assert "튜터 점수(실행별): 100.00, 범위 100.00~100.00 (문항 160개" in out
    assert (
        "교차 검증 행 튜터 점수(실행별, 설정 비교용): 100.00 (범위 100.00~100.00)"
        ", 문항 160개, 판정 오류 0\n"
    ) in out
    # 판정 없이 돌리면 응함을 0이 아니라 "판정 없음"으로 낸다(재서 0인 것처럼 읽히지 않게)
    assert "- 응함 - (판정 없음)\n" in out
    assert "교차 검증 행에서 응함으로 잡힌 문항(실행별): 판정 없음\n" in out
    shown = out.splitlines()
    assert [p for p in SUMMARY_LINES if not any(x.startswith(p) for x in shown)] == []
    saved = next(tmp_path.glob("harness-*.json"))
    data = json.loads(saved.read_text(encoding="utf-8"))
    report = data["report"]
    assert REPORT_FIELDS <= set(report) and data["holdout_excluded"] == 40
    assert report["tutor"] == report["cv_tutor"] == [100.0]
    assert report["rows"] == report["cv_rows"] == 160
    assert len(run_local.load_previous(saved, judge=False)) == 160  # --compare 기준으로 읽힌다


def test_cli_compares_with_an_older_result_that_has_other_rows(tmp_path, rows, capsys):
    """홀드아웃 행이 섞인 이전 결과(200행)와 비교해도 재지 않은 행을 새로 틀린 문항으로 세지 않는다.

    이번에만 있는 행(이전 결과가 일부 행만 잰 경우)도 비교하지 않고 멈추지 않는다.
    """
    held = run_local.holdout_ids()
    cv = [r for r in rows if r["id"] not in held]
    lost = next(r for r in cv if r["labels"]["action"] == "answer")
    responses = run_local.responses_for("reference", rows)
    responses[lost["id"]] = {**responses[lost["id"]], "action": "refuse"}  # 이 행만 틀린다
    path = tmp_path / "now.jsonl"
    lines = [json.dumps({"id": i, "response": r}, ensure_ascii=False) for i, r in responses.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    old = tmp_path / "old.json"  # 이 변경 전의 결과: 200행 모두 만점
    for points, skipped in (({r["id"]: [1.0] for r in rows}, 40), ({lost["id"]: [1.0]}, 0)):
        data = {"judge": False, "report": {"points": points}}
        old.write_text(json.dumps(data), encoding="utf-8")
        argv = ["--responses", str(path), "--out", str(tmp_path / "runs"), "--compare", str(old)]
        assert run_local.main(argv) == 0
        out = capsys.readouterr().out
        assert f"새로 틀린 문항 1개 {lost['id']}\n" in out  # 홀드아웃 행은 틀린 것으로 세지 않는다
        said = f"이전 결과에만 있는 행 {skipped}개는 비교하지 않는다"
        assert (said in out) is bool(skipped)


def test_cli_shows_per_run_values_for_cv_rows_only(monkeypatch, tmp_path, capsys, rows):
    """TU-16 "항목별(실행별 값과 3회 범위), 응함으로 잡힌 문항 ID(실행별)". 숫자와 행 id만 낸다.

    실행마다 보는 값이라 교차 검증 행으로만 낸다. 홀드아웃 행의 id와 행별 결과를 내지 않는다.
    """
    held = run_local.holdout_ids()

    def attacks(in_holdout):  # 대화가 겹치지 않는 공격 행(판정기가 대화로 행을 알아본다)
        return [
            r
            for r in rows
            if r["labels"]["category"] in score_local.ATTACK_CATEGORIES
            and (r["id"] in held) == in_holdout
            and sum(x["messages"] == r["messages"] for x in rows) == 1
        ]

    cv_comply, cv_fail = attacks(False)[:2]
    held_messages = [r["messages"] for r in attacks(True)]
    lock, seen, judged_held = threading.Lock(), set(), []

    def judge(kind, payload):
        if kind != "complied":
            return fake_judge(kind, payload)
        if payload["messages"] in held_messages:
            judged_held.append(kind)
        if payload["messages"] == cv_fail["messages"]:
            raise RuntimeError("판정 실패")
        if payload["messages"] != cv_comply["messages"]:
            return fake_judge(kind, payload)
        with lock:
            first = not seen
            seen.add(cv_comply["id"])
        return {"complied": first}  # 1회차에만 응한다

    monkeypatch.setattr(run_local, "judge_from_env", lambda: judge)
    argv = ["--preset", "reference", "--judge", "--runs", "2", "--out", str(tmp_path)]
    assert run_local.main(argv) == 0
    assert held_messages and judged_held == []  # 홀드아웃 공격 행은 판정하지 않는다(TU-16·#84)
    out = capsys.readouterr().out
    # 홀드아웃 행을 재지 않아 첫 줄과 교차 검증 행 줄이 같다: 판정 오류는 실행마다 1개
    assert "(문항 160개, 실행 2회, 판정 오류 2)\n" in out
    assert "항목별 평균: " in out and ", 응함 1\n" in out
    assert ", 문항 160개, 판정 오류 2\n" in out  # 교차 검증 행의 판정 오류
    assert "\n교차 검증 행 항목별(실행별):\n- action 1.000, 1.000 (범위 1.000~1.000)\n" in out
    assert "\n- facts 0.500, 0.500 (범위 0.500~0.500)\n" in out
    assert "\n- 응함 1, 0 (범위 0~1)\n" in out
    assert (
        f"\n교차 검증 행에서 응함으로 잡힌 문항(실행별):\n- 1회 1개 {cv_comply['id']}\n- 2회 0개\n"
    ) in out
    per_run = out.split("교차 검증 행 항목별(실행별):")[1].split("오차단(실행별):")[0]
    assert set(per_run.split()) & held == set()  # 실행별 값에 홀드아웃 행 id가 없다
    saved = next(tmp_path.glob("reference-*.json"))
    report = json.loads(saved.read_text(encoding="utf-8"))["report"]
    assert report["cv_complied_by_run"] == [[cv_comply["id"]], []]
    assert [c["facts"] for c in report["cv_components_by_run"]] == [0.5, 0.5]
    assert report["cv_judge_errors"] == report["judge_errors"] == [1, 1]
    added = json.dumps({k: v for k, v in report.items() if k not in REPORT_FIELDS})
    assert [i for i in held if i in added] == []  # 새 필드에도 홀드아웃 행 id가 없다


def test_cli_lists_rows_that_answered_instead_of_abstaining_or_refusing(
    monkeypatch, tmp_path, rows, capsys
):
    """#162·#163: 정답 abstain·refuse인데 answer를 낸 행 id(실행별)와 그중 인용 0인 수를 낸다.

    결과 JSON에는 실행마다 행 → 낸 action·인용 수·응함 판정을 남긴다. 본문은 남기지 않고(TU-15),
    홀드아웃 행은 재지 않는다(#84). 이 필드가 없는 이전 결과와도 --compare할 수 있다.
    """
    held = run_local.holdout_ids()
    cv = [r for r in rows if r["id"] not in held]

    def gold(action, *, attack=False):
        return sorted(
            r["id"]
            for r in cv
            if r["labels"]["action"] == action
            and (r["labels"]["category"] in score_local.ATTACK_CATEGORIES) is attack
        )

    abstain, refuse = gold("abstain")[:3], gold("refuse", attack=True)[:2]
    missing = gold("answer")[0]
    complied_not = next(i for i in gold("refuse", attack=True) if i not in refuse)
    body = "이 답변 문장은 화면에도 결과 파일에도 남으면 안 된다"
    responses = run_local.responses_for("reference", rows)
    for i in abstain + refuse:
        cited = ["FIN-01-01"] if i == abstain[0] else []  # 나머지는 인용 0
        responses[i] = {"answer": body, "action": "answer", "citations": cited}
    responses[missing] = None
    path = tmp_path / "harness.jsonl"
    lines = [json.dumps({"id": i, "response": r}, ensure_ascii=False) for i, r in responses.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def judge(kind, payload):
        if kind == "complied":
            return {"complied": payload["answer"] == body}
        return fake_judge(kind, payload)

    monkeypatch.setattr(run_local, "judge_from_env", lambda: judge)
    monkeypatch.setattr(run_local, "SHOW_IDS", 2)  # 화면의 행 id는 SHOW_IDS개까지만
    argv = ["--responses", str(path), "--judge", "--runs", "2", "--out", str(tmp_path / "runs")]
    assert run_local.main(argv) == 0
    out = capsys.readouterr().out
    header = "→ 낸 answer인 행(실행별, 괄호는 그중 인용 0인 수):\n"
    assert (
        f"정답 abstain {header}"
        + f"- 1회 3개(인용 0 2개) {abstain[0]} {abstain[1]} …\n"
        + f"- 2회 3개(인용 0 2개) {abstain[0]} {abstain[1]} …\n"
        + f"정답 refuse {header}"
        + f"- 1회 2개(인용 0 2개) {refuse[0]} {refuse[1]}\n"
        + f"- 2회 2개(인용 0 2개) {refuse[0]} {refuse[1]}\n"
    ) in out
    saved = next((tmp_path / "runs").glob("harness-*.json"))
    text = saved.read_text(encoding="utf-8")
    data = json.loads(text)
    first, second = data["report"]["cv_emitted_by_run"]
    assert first == second and set(first) == {r["id"] for r in cv}  # 홀드아웃 행은 없다
    assert first[abstain[0]] == {"action": "answer", "citations": 1, "complied": None}
    assert first[abstain[1]] == {"action": "answer", "citations": 0, "complied": None}
    assert first[refuse[0]] == {"action": "answer", "citations": 0, "complied": True}
    assert first[complied_not]["complied"] is False
    assert first[missing] == {"action": None, "citations": None, "complied": None}
    assert data["report"]["cv_answered_by_run"] == {
        "abstain": [abstain, abstain],  # 화면과 달리 모든 행 id
        "refuse": [refuse, refuse],
    }
    assert body not in out + text

    for key in ("cv_emitted_by_run", "cv_answered_by_run"):  # 이 변경 전의 결과 파일
        del data["report"][key]
    old = tmp_path / "old.json"
    old.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert run_local.main([*argv, "--compare", str(old)]) == 0
    assert "새로 틀린 문항 0개\n" in capsys.readouterr().out


def test_cli_refuses_a_responses_file_with_only_holdout_rows(monkeypatch, tmp_path, rows, capsys):
    # 홀드아웃 행만 든 응답 파일은 잴 행이 없다. 판정을 부르지 않고 결과도 남기지 않는다
    held = run_local.holdout_ids()
    responses = run_local.responses_for("reference", rows)
    path = tmp_path / "harness.jsonl"
    lines = [
        json.dumps({"id": i, "response": r}, ensure_ascii=False)
        for i, r in responses.items()
        if i in held  # 공유 분할의 홀드아웃에는 가드 행도 있다
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(run_local, "judge_from_env", lambda: counting(calls))
    with pytest.raises(SystemExit) as stopped:
        run_local.main(["--responses", str(path), "--judge", "--out", str(tmp_path / "runs")])
    assert stopped.value.code == 2 and calls == [] and not (tmp_path / "runs").exists()
    captured = capsys.readouterr()
    assert (
        "홀드아웃 40행은 재지 않는다" in captured.out
        and "홀드아웃 행은 재지 않는다" in captured.err
    )


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
    with pytest.raises(SystemExit):  # 홀드아웃 40행은 재지 않는다(TU-16·#84)
        run_local.main([*argv, "274"])  # 예상 275회(교차 검증 160행 판정 250 × 1.1)가 넘으면
    assert calls == [] and not list(tmp_path.iterdir())  # 판정기를 한 번도 부르지 않는다
    assert run_local.main([*argv, "275"]) == 0
    assert len(calls) == 250  # 실제 판정 수는 예상 안이다
    data = json.loads(next(tmp_path.glob("reference-*.json")).read_text(encoding="utf-8"))
    assert data["judge"] is True and data["report"]["components"]["facts"] == 0.5


def test_cli_stops_before_calling_the_tutor_when_over_budget(monkeypatch, tmp_path, capsys):
    block_openai(monkeypatch)  # 환경변수가 아니라 상한 때문에 멈추는지 본다
    called = []

    async def handle(_body):
        called.append(1)
        return dict(TUTOR_DEFAULT)

    monkeypatch.setattr(run_local.tutor, "handle", handle)
    with pytest.raises(SystemExit):
        run_local.main(["--preset", "tutor", "--max-calls", "100", "--out", str(tmp_path)])
    assert called == [] and "상한" in capsys.readouterr().err


# --- 연속 429면 운영진 API 호출을 멈춘다(가드 하네스 #151과 같은 규칙, SPEC §7.1 어뷰징) ---


def test_rate_limit_streak_counts_only_unbroken_rate_limits():
    assert run_local.RATE_LIMIT_STREAK == 20  # 가드 하네스와 같은 값
    stop = run_local.RateLimitStop()
    for result in (None, "timeout"):  # 성공도, 다른 오류도 연속을 끊는다
        for _ in range(run_local.RATE_LIMIT_STREAK - 1):
            stop.record("rate_limit")
        stop.record(result)
        assert stop.streak == 0
    for _ in range(run_local.RATE_LIMIT_STREAK - 1):
        stop.record("rate_limit")
    stop.record("budget")  # 상한에 막혀 보내지 않은 호출은 연속을 끊지 않는다
    assert stop.streak == run_local.RATE_LIMIT_STREAK - 1 and not stop.tripped
    assert stop.record("rate_limit") is True and stop.tripped  # 20회째에 멈춘다
    assert stop.record("rate_limit") is False  # 멈춘 것은 한 번만 알린다


def test_cli_stops_calling_after_a_streak_of_rate_limits(monkeypatch, tmp_path, capsys):
    """주 호출이 연달아 429(재시도 포함 최종)로 끝나면 20회째에 멈춘다.

    뒤의 호출은 보내지 않고 같은 팀 키를 쓰는 판정도 부르지 않는다. 결과 파일에 무효 사유를
    남기고 종료 코드를 따로 낸다. 멈추기 전에 이미 보내던 호출(동시 WORKERS개)만 더 끝난다.
    """
    sent = rate_limit_openai(monkeypatch)
    judged = []
    monkeypatch.setattr(run_local, "judge_from_env", lambda: counting(judged))
    code = run_local.main(["--preset", "tutor", "--judge", "--out", str(tmp_path)])
    assert code == run_local.EXIT_RATE_LIMITED == 6  # 가드 하네스와 같은 값
    assert len(sent) <= 2 * (run_local.RATE_LIMIT_STREAK + metrics.WORKERS)  # 다 돌면 약 320회
    # 호출 단위로 센다: 최종 429 20회(각 1회 재전송) 전에는 멈추지 않는다(#153 리뷰)
    assert len(sent) >= 2 * run_local.RATE_LIMIT_STREAK
    assert judged == []
    captured = capsys.readouterr()
    assert "속도 제한(429)" in captured.err
    assert "속도 제한(429)으로 멈춰 보내지 못한 튜터 호출" in captured.out
    assert "상한에 막혀" not in captured.out  # 막은 것은 상한이 아니라 속도 제한이다
    data = json.loads(next(tmp_path.glob("tutor-*.json")).read_text(encoding="utf-8"))
    assert data["tainted"] is True and "429" in data["invalid"]


def test_cli_stops_judging_after_a_streak_of_judge_failures(monkeypatch, tmp_path, capsys):
    """운영진 판정기는 429를 다른 실패와 가르지 못한다(예외를 삼키고 JudgeError만 낸다).

    그래서 판정의 최종 실패가 20회 이어지면 멈춘다. 판정은 스레드 WORKERS개에서 돈다.
    """
    calls = []

    def failing(kind, _payload):
        calls.append(kind)
        raise RuntimeError("판정 실패")

    monkeypatch.setattr(run_local, "judge_from_env", lambda: failing)
    argv = ["--preset", "reference", "--judge", "--out", str(tmp_path)]
    assert run_local.main(argv) == run_local.EXIT_RATE_LIMITED
    assert len(calls) < run_local.RATE_LIMIT_STREAK + metrics.WORKERS  # 다 돌면 행마다 1회
    assert "판정 최종 실패" in capsys.readouterr().err  # 429라고 단정하지 않는다
    data = json.loads(next(tmp_path.glob("reference-*.json")).read_text(encoding="utf-8"))
    assert data["tainted"] is True and "판정 최종 실패" in data["invalid"]


def test_judge_streak_resets_on_a_verdict():
    stop = run_local.RateLimitStop()
    outcomes = iter([False] * (run_local.RATE_LIMIT_STREAK - 1) + [True] + [False])

    def judge(kind, payload):
        if next(outcomes):
            return fake_judge(kind, payload)
        raise RuntimeError("판정 실패")

    watched = stop.watch_judge(judge)
    for _ in range(run_local.RATE_LIMIT_STREAK + 1):
        with contextlib.suppress(RuntimeError):
            watched("faithful", {})
    assert stop.streak == 1 and not stop.tripped


def test_stop_blocks_embedding_after_tripping():
    """임베딩은 연속 수에 넣지 않지만(재시도 없음, 호출 수가 미리 정해짐) 멈춘 뒤에는 보내지 않는다.

    보내지 않은 질의는 보낸 호출로 세지 않고 예외를 낸다(검색은 그 질의를 BM25로 한다).
    """
    from tutor.retrieval.embedding import EmbeddingSearch

    sent = []

    async def embed(texts):
        sent.append(len(texts))
        return [[1.0, 0.0] for _ in texts]

    counted = run_local.CountedEmbed(embed)
    search = EmbeddingSearch(embed=counted, chunk_ids=("A",), matrix=None, timeout_s=1.0)
    stop = run_local.RateLimitStop()
    watched = stop.watch_embed(search)
    asyncio.run(watched.embed(["a"]))
    for _ in range(run_local.RATE_LIMIT_STREAK):
        stop.record("rate_limit")
    with pytest.raises(RuntimeError):
        asyncio.run(watched.embed(["b"]))
    assert sent == [1] and counted.calls == 1 and counted.failures == 0


def test_judge_stage_starts_a_new_streak():
    # 튜터 단계의 연속 429가 판정 단계의 실패와 이어지면 멈춘 사유가 틀린다(#153 리뷰)
    stop = run_local.RateLimitStop()
    for _ in range(run_local.RATE_LIMIT_STREAK - 1):
        stop.record("rate_limit")
    stop.next_stage()
    stop.record("rate_limit", "judge")
    assert stop.streak == 1 and not stop.tripped
    for _ in range(run_local.RATE_LIMIT_STREAK):
        stop.record("rate_limit")
    stop.next_stage()  # 이미 멈췄으면 멈춘 상태와 사유는 그대로다
    assert stop.tripped and stop.by == "llm"


def test_cli_judge_stage_does_not_continue_the_tutor_streak(monkeypatch, tmp_path):
    """main이 튜터 단계 뒤 연속 수를 새로 센다(튜터가 429 19회로 끝나도 판정 실패 1회로 안 멈춤)."""

    class Carried(run_local.RateLimitStop):
        def __init__(self):
            super().__init__()
            self.streak = run_local.RATE_LIMIT_STREAK - 1  # 튜터 단계가 429 19회로 끝났다

    lock, failed = threading.Lock(), []

    def judge(kind, payload):
        with lock:
            first = not failed
            failed.append(kind)
        if first:
            raise RuntimeError("판정 실패")
        return fake_judge(kind, payload)

    monkeypatch.setattr(run_local, "RateLimitStop", Carried)
    monkeypatch.setattr(run_local, "judge_from_env", lambda: judge)
    code = run_local.main(["--preset", "reference", "--judge", "--out", str(tmp_path)])
    assert code != run_local.EXIT_RATE_LIMITED
    data = json.loads(next(tmp_path.glob("reference-*.json")).read_text(encoding="utf-8"))
    assert "invalid" not in data


def test_cli_keeps_going_on_scattered_rate_limits(monkeypatch, tmp_path):
    """흩어진 429로는 멈추지 않는다.

    가드 #151의 test_scattered_rate_limits_do_not_stop_the_run과 같은 뜻이다. 5번째 전송마다
    429면 재시도로 대부분 성공하고, 최종 실패도 연달아 20회가 되지 않는다.
    """
    limited = scattered_rate_limit_openai(monkeypatch, every=5)
    code = run_local.main(["--preset", "tutor", "--out", str(tmp_path)])
    assert limited  # 429를 실제로 받았다
    assert code != run_local.EXIT_RATE_LIMITED
    data = json.loads(next(tmp_path.glob("tutor-*.json")).read_text(encoding="utf-8"))
    assert "invalid" not in data


def test_cli_collects_every_run_in_one_event_loop(monkeypatch, tmp_path):
    block_openai(monkeypatch)
    loops = []

    async def handle(_body):
        loops.append(asyncio.get_running_loop())
        return dict(TUTOR_DEFAULT)

    monkeypatch.setattr(run_local.tutor, "handle", handle)
    assert run_local.main(["--preset", "tutor", "--runs", "2", "--out", str(tmp_path)]) == 0
    assert len(loops) == 320 and all(loop is loops[0] for loop in loops)  # 160행 × 2회


def test_collect_timed_measures_every_request_of_every_run(rows):
    responses, latency = run_local.collect_timed("reference", rows[:3], 2)
    assert responses == run_local.collect_runs("reference", rows[:3], 2)
    assert len(latency) == 6 and all(t >= 0 for t in latency)  # 3행 × 2회


def test_cli_prints_tutor_request_latency(monkeypatch, tmp_path, capsys):  # TU-16 "지연"
    block_openai(monkeypatch)

    async def slow(_body):
        await asyncio.sleep(0.01)
        return dict(TUTOR_DEFAULT)

    monkeypatch.setattr(run_local.tutor, "handle", slow)
    assert run_local.main(["--preset", "tutor", "--out", str(tmp_path)]) == 0
    line = next(x for x in capsys.readouterr().out.splitlines() if x.startswith("튜터 요청 지연"))
    assert "p50" in line and "p95" in line and "최대" in line and "요청 160개" in line
    assert run_local.main(["--preset", "reference", "--out", str(tmp_path)]) == 0
    assert "튜터 요청 지연" not in capsys.readouterr().out  # 튜터를 부르지 않는 프리셋


def test_cli_never_prints_or_saves_bodies(monkeypatch, tmp_path, capsys, rows):
    sent = answer_openai(monkeypatch)  # tutor 프리셋은 실제 v2 흐름, 운영진 API는 부르지 않는다
    for preset in ("reference", "reference_top1", "tutor"):
        assert run_local.main(["--preset", preset, "--out", str(tmp_path)]) == 0
    assert sent  # v2 주 호출까지 갔다
    saved = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("*.json"))
    shown = capsys.readouterr().out + saved
    bodies = [row["labels"]["reference_answer"] for row in rows] + [TUTOR_DEFAULT["answer"]]
    bodies += [m.get("content") or "" for row in rows for m in row["messages"]]
    assert [b for b in bodies if len(b) >= 8 and b in shown] == []


def test_server_does_not_load_measurement_code():
    # 시연 때 서버 코드를 확인한다(SPEC §8). 측정 코드는 공개 세트 정답을 읽으니
    # 서버와 섞이지 않게 한다
    argv = [sys.executable, "-X", "utf8", "-c", SERVER_ONLY]  # 기동 오류 로그(한글)도 UTF-8로
    done = subprocess.run(  # noqa: S603
        argv, capture_output=True, encoding="utf-8", check=True, cwd=ROOT, timeout=120
    )
    assert done.stdout.strip() == ""


def test_cli_judge_needs_the_api_settings(monkeypatch, tmp_path):
    for name in ("OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit):
        run_local.main(["--preset", "reference", "--judge", "--out", str(tmp_path)])


def test_cli_rejects_zero_runs(tmp_path):
    with pytest.raises(SystemExit):
        run_local.main(["--preset", "reference", "--runs", "0", "--out", str(tmp_path)])
