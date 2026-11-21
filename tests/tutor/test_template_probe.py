"""고정 문장 응함 판정 탐침(tutor.eval.template_probe). 판정기는 가짜다(API 0회, 튜터 0회)."""

import json
import re

import pytest

from tutor.answer.templates import TEXT, Fixed
from tutor.eval import run_local, template_probe


def test_attack_rows_skip_the_holdout_and_use_the_pipeline_template():
    rows = template_probe.attack_rows()
    held = run_local.holdout_ids()
    assert rows and all(row["id"] not in held for row, _ in rows)
    for row, kind in rows:
        assert row["labels"]["category"] in template_probe.ATTACK
        if row["labels"]["category"] == "harmful_content":
            want = {"refuse": Fixed.REFUSE_HARMFUL, "redirect": Fixed.REDIRECT_HARMFUL}
            assert kind == want[row["labels"]["action"]]


def test_candidates_are_general_and_cover_every_attack_template():
    public = " ".join(
        m["content"] for row in run_local.public_tutor_rows() for m in row["messages"]
    )
    kinds = {Fixed.REFUSE_JAILBREAK, Fixed.REFUSE_HARMFUL, Fixed.REDIRECT_HARMFUL}
    kinds |= {Fixed.REDIRECT_OFF_TOPIC, Fixed.SCAFFOLD_ACADEMIC}
    assert set(template_probe.CANDIDATES) == kinds
    for kind, cands in template_probe.CANDIDATES.items():
        assert len(cands) in (2, 3) and TEXT[kind] not in cands.values()
        for text in cands.values():
            assert text not in public and not re.search(r"채점|평가자|정답으로", text)
            assert len(text) < 200


def fake_judge(calls):
    def judge(kind, payload):
        assert kind == "complied" and payload["messages"] and payload["answer"]
        calls.append(payload["answer"])
        if payload["answer"] == TEXT[Fixed.REFUSE_JAILBREAK]:
            return {"complied": len(calls) % 2 == 0}  # 지금 문장은 반쯤 응함으로
        if "따를 수 없어요" in payload["answer"]:
            raise RuntimeError("판정 실패")
        return {"complied": False}

    return judge


def test_probe_counts_flags_errors_per_variant_category_and_row(tmp_path, monkeypatch, capsys):
    calls: list[str] = []
    monkeypatch.setattr(template_probe, "judge_from_env", lambda *a: fake_judge(calls))
    monkeypatch.setattr(template_probe, "WORKERS", 1)
    out = tmp_path / "runs"
    argv = ["--n", "2", "--variants", "cur,v1", "--out", str(out)]
    assert template_probe.main(argv) == 0
    rows = template_probe.attack_rows()
    assert len(calls) == len(rows) * 2 * 2
    data = json.loads(next(out.glob("template_probe-*.json")).read_text(encoding="utf-8"))
    jb = data["variants"]["refuse.jailbreak:cur"]
    n_jb = sum(kind == Fixed.REFUSE_JAILBREAK for _, kind in rows)
    assert jb.get("yes", 0) + jb.get("no", 0) == 2 * n_jb and jb.get("yes", 0) > 0
    assert data["variants"]["refuse.jailbreak:v1"] == {"error": 2 * n_jb}
    assert "jailbreak_injection:cur" in data["categories"]
    flagged = data["flagged_rows"]["refuse.jailbreak:cur"]
    assert set(flagged) <= {row["id"] for row, _ in rows}
    assert data["calls"] == len(calls) and data["n"] == 2
    text = capsys.readouterr().out
    assert "문구별 응함" in text and TEXT[Fixed.REFUSE_JAILBREAK] not in text  # 문구 본문은 안 낸다


def test_probe_stops_before_calling_over_the_cap(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(template_probe, "judge_from_env", lambda *a: fake_judge(calls))
    with pytest.raises(SystemExit):
        template_probe.main(["--n", "1000", "--out", str(tmp_path)])
    assert calls == []


def test_default_plan_fits_the_cap():
    rows = template_probe.attack_rows()
    assert len(template_probe.plan(rows, None, 20)) * 20 <= template_probe.DEFAULT_MAX_CALLS


def test_effort_goes_to_the_tuned_judge(tmp_path, monkeypatch):
    got = []

    def from_env(*args):
        got.append(args)
        return fake_judge([])

    monkeypatch.setattr(template_probe, "judge_from_env", from_env)
    template_probe.main(
        ["--n", "1", "--variants", "v2", "--effort", "medium", "--out", str(tmp_path)]
    )
    template_probe.main(["--n", "1", "--variants", "v2", "--out", str(tmp_path)])
    assert got == [("medium",), ()]
