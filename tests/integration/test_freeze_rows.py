"""harness.freeze_rows: 동결 후보 실행의 T 고정 문장·normal 사실 행 점검(API 0회)."""

import json

import pytest

from harness import freeze_rows
from tutor.eval.run_local import public_tutor_rows


def _first(category):
    return next(r for r in public_tutor_rows() if r["labels"]["category"] == category)


def _write(tmp_path, name, emitted, traces):
    data = {"report": {"cv_emitted_by_run": emitted}, "row_trace": {"by_run": traces}}
    path = tmp_path / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_counts_intent_complied_and_normal_fact_losses(tmp_path, capsys):
    normal, harmful = _first("normal"), _first("harmful_content")
    gold_sid = normal["labels"]["evidence"][0]
    n, h = normal["id"], harmful["id"]
    run1 = {
        n: {"action": "answer", "facts": 0.5, "faithful": False, "cited": [gold_sid]},
        h: {"action": "redirect", "complied": True},
    }
    run2 = {
        n: {"action": "answer", "facts": 1.0, "faithful": True, "cited": [gold_sid]},
        h: {"action": "redirect", "complied": False},
    }
    run3 = {
        n: {"action": "answer", "facts": 0.0, "faithful": True, "cited": []},
        h: {"action": "refuse", "complied": False},
    }
    trace = {n: {"intent": "학습 질문"}, h: {"intent": "유해-비하"}}
    a = _write(tmp_path, "a.json", [run1, run2], [trace, trace])
    b = _write(tmp_path, "b.json", [run3], [{**trace, h: {"intent": "유해-실행"}}])

    assert freeze_rows.main([str(a), str(b)]) == 0
    out = capsys.readouterr().out
    assert "- 유해-비하 → redirect: 2 · 응함 1" in out
    assert "- 유해-실행 → refuse: 1 · 응함 0" in out
    assert f"응함 유해-비하→redirect: {h}(harmful_content/" in out
    assert "학습 질문 →" not in out
    assert f"- {n}: 잃음 2/3 · 평균 사실 0.25 · 정답 근거 인용 1 · 충실 실패 1" in out
    assert f"절반 넘게 잃은 행 1개: {n}" in out


def test_rejects_results_without_row_trace(tmp_path):
    path = tmp_path / "x.json"
    path.write_text(json.dumps({"report": {"cv_emitted_by_run": [{}]}}), encoding="utf-8")
    with pytest.raises(SystemExit):
        freeze_rows.main([str(path)])


def test_prints_self_check_reasons_when_present(tmp_path, capsys):
    normal = _first("normal")
    n = normal["id"]
    run = {n: {"action": "answer", "facts": 1.0, "faithful": True, "cited": []}}
    a = _write(
        tmp_path,
        "a.json",
        [run, run],
        [{n: {"self_check": "rewrote"}}, {n: {"self_check": "kept"}}],
    )
    b = _write(tmp_path, "b.json", [run], [{n: {"intent": None}}])
    freeze_rows.main([str(a)])
    assert "2차 자체 검증(행×실행): " in capsys.readouterr().out
    freeze_rows.main([str(b)])
    assert "2차 자체 검증" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("cited", "gold", "want"),
    [
        (["A"], ["A"], ("exact", 1.0)),
        (["A", "B"], ["A"], ("extra", 2 / 3)),
        (["A"], ["A", "B"], ("partial", 2 / 3)),
        (["B"], ["A"], ("wrong", 0.0)),
        ([], ["A"], ("none", 0.0)),
    ],
)
def test_cite_kind(cited, gold, want):
    kind, f1 = freeze_rows.cite_kind(cited, gold)
    assert (kind, round(f1, 6)) == (want[0], round(want[1], 6))


def test_more_prints_citation_and_action_tables(tmp_path, capsys):
    normal, oom = _first("normal"), _first("out_of_material")
    n, o = normal["id"], oom["id"]
    sid = normal["labels"]["evidence"][0]
    run1 = {n: {"action": "answer", "facts": 1.0, "faithful": True, "cited": [sid]},
            o: {"action": "answer", "cited": []}}  # fmt: skip
    run2 = {n: {"action": "answer", "facts": 1.0, "faithful": True, "cited": ["ZZ-99-99"]},
            o: {"action": "answer", "cited": []}}  # fmt: skip
    a = _write(tmp_path, "a.json", [run1, run2], [{}, {}])
    freeze_rows.main([str(a), "--more"])
    out = capsys.readouterr().out
    assert "exact 1(잃은 F1 0.0)" in out and "wrong 1(잃은 F1 1.0)" in out
    assert "- out_of_material/abstain: answer 2" in out
    assert f"action이 절반 넘게 틀린 행 1개: {o}(out_of_material/abstain) 2" in out
