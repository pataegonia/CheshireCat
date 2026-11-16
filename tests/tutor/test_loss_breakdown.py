"""행별 잃은 점수(loss_breakdown)와 측정 전용 행 기록(flow.ROW_PROBE → run_local row_trace)."""

import json

import pytest

from integration.actions import FinalizeConfig
from integration.contracts import Sentence
from tests.tutor.test_flow import QUESTION, FakeLLM, ask, out, state_with
from tests.tutor.tutor_testlib import block_openai
from tutor.eval import loss_breakdown as lb
from tutor.eval import run_local
from tutor.judge import flow
from tutor.judge.main_call import EvidenceOut, SentenceOut
from tutor.runtime.response import TUTOR_DEFAULT

GOLD = {row["id"]: row["labels"] for row in run_local.public_tutor_rows()}


def _first(pred):
    return next(i for i, labels in GOLD.items() if pred(labels))


A = _first(lambda g: g["action"] == "answer" and g.get("evidence") and g.get("gold_facts"))
B = _first(lambda g: g["action"] == "abstain" and not g.get("evidence"))
C = _first(lambda g: g["category"] == "jailbreak_injection")


def emitted(action, *, facts=None, faithful=None, complied=None):
    return {"action": action, "facts": facts, "faithful": faithful, "complied": complied}


def trace(main_call="ok", claims=None, unverified=None, dropped=None, default=False):
    return {
        "default": default,
        "main_call": main_call,
        "claims": claims,
        "unverified": unverified,
        "dropped": dropped,
    }


def run_json(with_trace=True):
    """2회 실행. 1회: A 사실 0.5(0.875) · B action 오답·불충실(0) · C 응함(0).
    2회: A 인용 F1 0(0.75) · B 맞음(1) · C 형식 오류(0)."""
    gold_a = GOLD[A]["action"]
    data = {
        "preset": "tutor",
        "report": {
            "tutor": [29.17, 58.33],
            "points": {A: [0.875, 0.75], B: [0.0, 1.0], C: [0.0, 0.0]},
            "cv_emitted_by_run": [
                {
                    A: emitted(gold_a, facts=0.5, faithful=True),
                    B: emitted("answer", faithful=False),
                    C: emitted("answer", complied=True),
                },
                {
                    A: emitted(gold_a, facts=1.0, faithful=True),
                    B: emitted("abstain", faithful=True),
                    C: emitted(None),
                },
            ],
        },
    }
    if with_trace:
        data["row_trace"] = {
            "by_run": [
                {A: trace(claims=2, unverified=1, dropped=0), B: trace(claims=1), C: trace()},
                {A: trace(claims=2, unverified=0), B: trace(), C: trace("failed", default=True)},
            ]
        }
    return data


# --- 한 행의 몫 ---


def test_row_loss_splits_by_part_count():
    labels = {"action": "answer", "evidence": ["PY-01-01"]}
    loss, bad = lb.row_loss(emitted("answer", facts=0.5, faithful=True), 0.875, labels)
    expected = {"action": 0, "facts": 0.125, "faithful": 0, "citation": 0}
    assert not bad and loss == pytest.approx(expected)
    loss, bad = lb.row_loss(emitted("answer", facts=1.0, faithful=True), 0.75, labels)
    assert not bad and loss["citation"] == pytest.approx(0.25)  # 인용 F1은 점수에서 뺀 값


def test_row_loss_gives_the_whole_row_to_complied_or_invalid():
    labels = {"action": "refuse", "evidence": []}
    assert lb.row_loss(emitted("answer", complied=True), 0.0, labels) == ({"complied": 1.0}, False)
    assert lb.row_loss(emitted(None), 0.0, labels) == ({"invalid": 1.0}, False)


def test_row_loss_without_judge_has_action_and_citation_only():
    labels = {"action": "answer", "evidence": ["PY-01-01"]}
    loss, bad = lb.row_loss(emitted("clarify"), 0.0, labels)
    assert not bad and loss == {"action": 0.5, "citation": 0.5}


def test_row_loss_flags_points_that_do_not_fit():
    labels = {"action": "abstain", "evidence": []}
    _loss, bad = lb.row_loss(emitted("abstain", faithful=True), 0.5, labels)
    assert bad


# --- 결과 JSON 집계 ---


def test_parts_add_up_to_the_lost_score():
    b = lb.breakdown(run_json(), GOLD)
    assert b.runs == 2 and b.score == pytest.approx(43.75) and b.mismatches == 0
    every = [run for runs in b.rows.values() for run in runs]
    total = lb._sum_parts(every, b.runs)
    assert sum(total.values()) == pytest.approx(100 - b.score)
    third = 100 / 3
    assert total == pytest.approx(
        {
            "action": third / 4,  # B 1회: 1/2 × 100/3, 실행 평균
            "citation": third / 8,  # A 2회: 1/4 × 100/3
            "facts": third / 16,  # A 1회: 0.5/4 × 100/3
            "faithful": third / 4,
            "complied": third / 2,  # C 1회 행 전체
            "invalid": third / 2,  # C 2회 행 전체
        }
    )


def test_cli_prints_categories_flags_and_top_rows_without_text(tmp_path, capsys):
    path = tmp_path / "tutor-x.json"
    path.write_text(json.dumps(run_json()), encoding="utf-8")
    assert lb.main([str(path), "--top", "2"]) == 0
    shown = capsys.readouterr().out
    assert "튜터 점수(실행 평균) 43.75 (결과 JSON 43.75)" in shown
    assert "잃은 점수(튜터 점수 점, 실행 평균): 56.25 = action 8.33 · citation 4.17" in shown
    cat_c = GOLD[C]["category"]
    assert f"- {cat_c} 1행: 33.33 = " in shown
    assert "모든 실행 합: 기본값 1/6 · 주 호출 실패 1/6 · claim 5 · 미확인 1 · 지움 0" in shown
    gold_c = f"{cat_c}/{GOLD[C]['action']}"
    assert f"- {C} {gold_c} 33.33 complied·invalid [기본값 1/2 · 주 호출 실패 1/2" in shown
    assert f"잃은 점수 상위 2행: {C} {B}\n" in shown


def test_old_result_without_row_trace_still_breaks_down(tmp_path, capsys):
    path = tmp_path / "old.json"
    path.write_text(json.dumps(run_json(with_trace=False)), encoding="utf-8")
    assert lb.main([str(path)]) == 0
    shown = capsys.readouterr().out
    assert "row_trace가 없는 결과다" in shown and "[기본값 -]" in shown


def test_result_without_row_records_is_refused(tmp_path, capsys):
    data = run_json()
    del data["report"]["cv_emitted_by_run"]
    path = tmp_path / "older.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SystemExit):
        lb.main([str(path)])
    assert "cv_emitted_by_run" in capsys.readouterr().err


# --- 측정 전용 행 기록 ---

BAD = SentenceOut(
    text="9회까지예요.", kind="claim", evidence=[EvidenceOut(sid="PY-01-01", quote="9회까지")]
)


def _probe(state, *texts):
    noted = {}
    token = flow.ROW_PROBE.set(noted)
    try:
        resp = ask(state, *texts)
    finally:
        flow.ROW_PROBE.reset(token)
    return resp, noted


def test_probe_counts_claims_and_unverified_quotes():
    llm = FakeLLM(out(sentences=[*out().sentences, BAD]))
    resp, noted = _probe(state_with(llm), QUESTION)
    assert noted == {"main_call": "ok", "claims": 2, "unverified": 1, "dropped": 0}
    assert resp == ask(state_with(llm), QUESTION)  # 기록해도 응답은 같다(서버는 기록하지 않음)


def test_probe_marks_main_call_failure():
    _resp, noted = _probe(state_with(FakeLLM(error="timeout")), QUESTION)
    assert noted == {"main_call": "failed"}


def test_probe_is_off_by_default():
    assert flow.ROW_PROBE.get() is None
    ask(state_with(FakeLLM(out())), QUESTION)  # 기록할 곳이 없어도 그대로 돈다


def test_claim_counts_sees_claims_cut_from_the_answer():
    def sentence(text):
        return Sentence.model_validate({"text": text, "kind": "claim", "evidence": []})

    cfg = FinalizeConfig(unverified_notice="안내", refuse_text="거절")
    claims = [sentence("남은 문장."), sentence("지운 문장.")]
    counts = flow.claim_counts(claims, [], cfg, "남은 문장.")
    assert counts == {"claims": 2, "unverified": 2, "dropped": 1}


def test_run_local_writes_row_trace_per_row(monkeypatch, tmp_path, capsys):
    block_openai(monkeypatch)
    rows = run_local.public_tutor_rows()
    default_row = next(r["id"] for r in rows if r["id"] not in run_local.holdout_ids())

    async def respond(_preset, row, _state=None):
        if row["id"] == default_row:
            flow._note(main_call="failed")
            return dict(TUTOR_DEFAULT)
        flow._note(main_call="ok", claims=len(row["id"]) % 3, unverified=0, dropped=0)
        labels = row["labels"]
        cited = labels.get("evidence") or []
        return {"answer": "x", "action": labels["action"], "citations": cited}

    monkeypatch.setattr(run_local, "_respond", respond)
    assert run_local.main(["--preset", "tutor", "--runs", "2", "--out", str(tmp_path)]) == 0
    saved = next(tmp_path.glob("tutor-*.json"))
    data = json.loads(saved.read_text(encoding="utf-8"))
    first, second = data["row_trace"]["by_run"]
    assert first == second and len(first) == 160
    assert first[default_row] == trace("failed", default=True)
    other = next(i for i in first if i != default_row)
    assert first[other] == trace(claims=len(other) % 3, unverified=0, dropped=0)
    capsys.readouterr()
    assert lb.main([str(saved)]) == 0
    assert "주 호출 실패 2/320" in capsys.readouterr().out
