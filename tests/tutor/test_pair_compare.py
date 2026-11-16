"""행 단위 짝 비교(tutor.eval.pair_compare)."""

import json

from tutor.eval import pair_compare as pc


def write(tmp_path, name, points):
    path = tmp_path / name
    path.write_text(json.dumps({"report": {"points": points}}), encoding="utf-8")
    return path


def test_row_means_average_runs_and_files_and_skip_none(tmp_path):
    a = write(tmp_path, "a.json", {"r1": [1.0, 0.0], "r2": [None, 0.5]})
    b = write(tmp_path, "b.json", {"r1": [1.0], "r3": [None]})
    assert pc.row_means([a, b]) == {"r1": 2 / 3, "r2": 0.5}


def test_compare_counts_fixed_and_broken_rows_by_threshold():
    base = {"r1": 0.0, "r2": 1.0, "r3": 0.5, "r4": 0.5}
    variant = {"r1": 1.0, "r2": 0.5, "r3": 0.6, "r5": 1.0}
    cats = {"r1": "out_of_material", "r2": "normal"}
    got = pc.compare(base, variant, cats, threshold=0.25)
    assert got == {
        "fixed": [("r1", "out_of_material", 1.0)],
        "broken": [("r2", "normal", -0.5)],
    }


def test_main_prints_counts_without_text(tmp_path, capsys):
    cases = tmp_path / "cases.jsonl"
    cases.write_text(json.dumps({"id": "r1", "labels": {"category": "normal"}}) + "\n")
    base = write(tmp_path, "b.json", {"r1": [0.0]})
    var = write(tmp_path, "v.json", {"r1": [1.0]})
    assert pc.main(["--base", str(base), "--variant", str(var), "--cases", str(cases)]) == 0
    out = capsys.readouterr().out
    assert "고친 행 1 · 망친 행 0 · 순이득 +1" in out and "r1(normal,+1.00)" in out
