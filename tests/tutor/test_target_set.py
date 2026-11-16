"""목표 세트(측정 전용): 고르는 규칙과 run_local --cases <경로>의 공개 부분 세트 측정(API 0회)."""

import json

import pytest

from tutor.eval import run_local, target_set
from tutor.eval.grader import PUBLIC_DIR

PUBLIC = PUBLIC_DIR / "tutor.jsonl"


@pytest.fixture(scope="module")
def public_lines():
    return [line for line in PUBLIC.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def target(tmp_path):
    path = tmp_path / "target.jsonl"
    counts = target_set.build(path)
    return path, counts


def test_target_set_is_cv_abstain_plus_ten_normal_answers_per_subject(target, public_lines):
    path, counts = target
    assert counts == {"abstain": 16, "PY": 10, "PR": 10, "FIN": 10}
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 46
    assert [ln for ln in public_lines if ln in set(lines)] == lines  # 공개 줄 그대로, 공개 순서
    rows = [json.loads(line) for line in lines]
    held = run_local.holdout_ids()
    assert not {row["id"] for row in rows} & held  # 교차 검증 행만
    for row in rows:
        labels = row["labels"]
        if labels["action"] != "abstain":
            assert (labels["action"], labels["category"]) == ("answer", "normal")


def test_target_set_is_the_same_every_time(tmp_path, target):
    path, _ = target
    again = tmp_path / "again.jsonl"
    target_set.build(again)
    assert again.read_bytes() == path.read_bytes()


def test_pick_refuses_a_subject_with_too_few_rows():
    rows = [
        {"id": f"r{i}", "labels": {"action": "answer", "category": "normal", "evidence": [sid]}}
        for i, sid in enumerate(["PY-01-01"] * 10 + ["PR-01-01"] * 10 + ["FIN-01-01"] * 9)
    ]
    with pytest.raises(ValueError, match="FIN"):
        target_set.pick(rows, held=set())


def test_cli_prints_counts_only(tmp_path, capsys):
    out = tmp_path / "t.jsonl"
    assert target_set.main(["--out", str(out)]) == 0
    shown = capsys.readouterr().out
    assert shown.startswith("목표 세트 46행(abstain 16, PY 10, PR 10, FIN 10): ")
    assert "content" not in shown


def test_run_local_measures_the_target_file_as_a_public_subset(target, tmp_path, capsys):
    path, _ = target
    runs = tmp_path / "runs"
    assert run_local.main(["--preset", "reference", "--cases", str(path), "--out", str(runs)]) == 0
    out = capsys.readouterr().out
    assert "공개 부분 세트 target.jsonl: 공개 튜터 행 46개를 잰다\n" in out
    assert "튜터 점수(실행별): 100.00, 범위 100.00~100.00 (문항 46개" in out
    (saved,) = runs.glob("reference-subset-*.json")
    data = json.loads(saved.read_text(encoding="utf-8"))
    assert "cases" not in data  # 공개 행이라 공개 세트 결과와 행별로 비교한다
    assert data["subset"]["file"] == "target.jsonl" and data["subset"]["rows"] == 46
    assert len(data["subset"]["ids_sha256"]) == 16
    assert data["report"]["rows"] == 46


def test_subset_drops_holdout_rows(tmp_path, public_lines, capsys):
    held = run_local.holdout_ids()
    rows = [json.loads(line) for line in public_lines]
    one_held = next(i for i, r in enumerate(rows) if r["id"] in held)
    two_cv = [i for i, r in enumerate(rows) if r["id"] not in held][:2]
    path = tmp_path / "mixed.jsonl"
    path.write_text("".join(public_lines[i] + "\n" for i in [one_held, *two_cv]), "utf-8")
    runs = tmp_path / "runs"
    assert run_local.main(["--preset", "reference", "--cases", str(path), "--out", str(runs)]) == 0
    assert "홀드아웃 1행은 재지 않는다" in capsys.readouterr().out
    data = json.loads(next(runs.glob("*.json")).read_text(encoding="utf-8"))
    assert data["holdout_excluded"] == 1 and data["report"]["rows"] == 2


def test_subset_compares_with_a_full_public_run(target, tmp_path, capsys):
    path, _ = target
    full, sub = tmp_path / "full", tmp_path / "sub"
    assert run_local.main(["--preset", "reference", "--out", str(full)]) == 0
    previous = next(full.glob("*.json"))
    argv = ["--preset", "reference", "--cases", str(path), "--out", str(sub)]
    assert run_local.main([*argv, "--compare", str(previous)]) == 0
    out = capsys.readouterr().out
    assert "새로 틀린 문항 0개\n" in out
    assert "이전 결과에만 있는 행 114개는 비교하지 않는다\n" in out


@pytest.mark.parametrize("case", ["changed", "mixed", "twice"])
def test_subset_file_must_hold_unchanged_public_rows(tmp_path, public_lines, case, capsys):
    first = json.loads(public_lines[0])
    if case == "changed":
        first["labels"]["action"] = "refuse"
        lines = [json.dumps(first, ensure_ascii=False)]
    elif case == "mixed":  # 공개 행이 아닌 줄이 앞에 와도 섞인 파일로 막는다
        other = {**first, "id": "team-x", "axis": "premise"}
        lines = [json.dumps(other, ensure_ascii=False), public_lines[0]]
    else:
        lines = [public_lines[0], public_lines[0]]
    path = tmp_path / "bad.jsonl"
    path.write_text("".join(f"{ln}\n" for ln in lines), encoding="utf-8")
    argv = ["--preset", "reference", "--cases", str(path), "--out", str(tmp_path / "runs")]
    with pytest.raises(SystemExit):
        run_local.main(argv)
    assert "--cases 파일" in capsys.readouterr().err
    assert not (tmp_path / "runs").exists()
