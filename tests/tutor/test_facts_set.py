"""tutor.eval.facts_set: 사실 손실 행 + 대조 normal 행 부분 세트(API 0회)."""

import json

import pytest

from tutor.eval import facts_set
from tutor.eval.run_local import holdout_ids, public_tutor_rows


def _cv_normal_ids():
    rows = public_tutor_rows()
    held = holdout_ids(rows)
    return [r["id"] for r in rows if r["id"] not in held
            and (r["labels"]["category"], r["labels"]["action"]) == ("normal", "answer")]  # fmt: skip


def test_builds_ids_plus_deterministic_controls(tmp_path):
    ids = _cv_normal_ids()[:3]
    id_file = tmp_path / "ids.txt"
    id_file.write_text("# 주석\n" + "\n".join(f"{i}  # 메모" for i in ids) + "\n", encoding="utf-8")
    out1, out2 = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    assert facts_set.main(["--ids", str(id_file), "--out", str(out1), "--controls", "5"]) == 0
    facts_set.main(["--ids", str(id_file), "--out", str(out2), "--controls", "5"])
    got = [json.loads(line)["id"] for line in out1.read_text(encoding="utf-8").splitlines()]
    assert len(got) == 8 and set(ids) <= set(got)
    assert out1.read_text(encoding="utf-8") == out2.read_text(encoding="utf-8")
    public = {r["id"]: r for r in public_tutor_rows()}
    for line in out1.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        assert row == public[row["id"]]  # 공개 파일 줄 그대로


def test_rejects_holdout_or_unknown_ids(tmp_path):
    rows = public_tutor_rows()
    held = sorted(holdout_ids(rows))
    for bad in (held[0], "pub-없는행"):
        id_file = tmp_path / "ids.txt"
        id_file.write_text(bad + "\n", encoding="utf-8")
        with pytest.raises(SystemExit):
            facts_set.main(["--ids", str(id_file), "--out", str(tmp_path / "o.jsonl")])
