"""tutor.eval.five_draft_report: rows JSONL만 읽는 비교 집계(API 0회)."""

import json

from tutor.eval import five_draft_report as rep


def row(run, rid, points, ok, gold="answer", **md):
    return {"run": run, "id": rid, "gold_action": gold, "points": points, "action_ok": ok, **md}


T5_ROW = {
    "md_n": 5,
    "md_ok": 4,
    "md_codes": ["-", "-", "-", "cut", "-"],
    "md_hints": ["used"],
    "md_pick": 4,
    "md_redraft": None,
    "md_valid3": 3,
    "md_pick3_action": "abstain",
    "md_pick_action": "answer",
}


def test_arm_summary_counts_states_hints_and_three_vs_five():
    rows = [row(0, "a", 1.0, 1.0, **T5_ROW), row(0, "b", 0.5, 0.0, gold="abstain", **T5_ROW)]
    s = rep.arm_summary(rows)
    assert s["score"] == [75.0]
    assert s["states"] == {"v": 8, "l": 2}
    assert s["hints"] == {"used": 2} and s["hint_picked"] == 2
    assert (s["split3"], s["fix3"], s["break3"]) == (2, 1, 1)
    assert s["valid"] == {4: 2}


def test_paired_diff_fix_and_break():
    base = [row(r, i, p, ok) for r in (0, 1) for i, p, ok in (("a", 0.5, 0.0), ("b", 1.0, 1.0))]
    cand = [row(r, i, p, ok) for r in (0, 1) for i, p, ok in (("a", 1.0, 1.0), ("b", 0.5, 0.0))]
    p = rep.paired(base, cand)
    assert p["n"] == 2 and p["diff"] == 0.0 and p["fix"] == 1 and p["break"] == 1
    lo, hi = p["ci95"]
    assert lo <= 0.0 <= hi


def test_main_joins_files_as_separate_runs(tmp_path, capsys):
    def write(name, rows):
        path = tmp_path / name
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return str(path)

    r1 = write("r1.jsonl", [row(0, "a", 1.0, 1.0)])
    r2 = write("r2.jsonl", [row(0, "a", 0.0, 0.0)])
    t1 = write("t1.jsonl", [row(0, "a", 1.0, 1.0, **T5_ROW)])
    assert rep.main([f"R={r1}", f"R={r2}", f"T5={t1}"]) == 0
    out = capsys.readouterr().out
    assert "| R | 2 | 2 | 50.00 ± 70.71" in out
    assert "- T5: 1행, 차이 +50.00" in out
