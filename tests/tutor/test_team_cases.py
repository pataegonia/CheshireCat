"""팀 작성 점검 세트(TU-16·TU-19, #80): 형식 검사, 축별 지표, run_local --cases.

문항 내용은 테스트용 자리표시뿐이다(점검 세트 문항은 팀이 쓰거나 nano로 만든다).
"""

import json

import pytest

from tutor.eval import metrics, run_local, team_cases
from tutor.eval.grader import KB_DIR, score_local

KB = score_local.load_kb_dir(KB_DIR)
BODY = "본문이 섞이면 안 되는 문장"  # 오류·출력에 문항 본문이 나오지 않는지 본다


def case(row_id="c-1", axis="paraphrase", action="answer", category="normal"):
    return {
        "id": row_id,
        "endpoint": "tutor",
        "axis": axis,
        "messages": [{"role": "user", "content": BODY}],
        "labels": {"action": action, "category": category},
    }


def write(tmp_path, rows, name="cases.jsonl"):
    path = tmp_path / name
    lines = [r if isinstance(r, str) else json.dumps(r, ensure_ascii=False) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_valid_cases_load_in_order(tmp_path):
    rows = [case("c-1"), case("c-2", axis="premise", action="abstain", category="out_of_material")]
    rows[1]["labels"]["evidence"] = ["FIN-03-01"]
    assert team_cases.load_cases(write(tmp_path, [rows[0], "", rows[1]]), KB) == rows


@pytest.mark.parametrize(
    "broken, field",
    [
        (lambda r: r.update(id=""), "id"),
        (lambda r: r.update(endpoint="guard"), "endpoint"),
        (lambda r: r.update(axis="other"), "axis"),
        (lambda r: r.update(note="x"), "note"),  # 모르는 필드
        (lambda r: r.update(messages=[]), "messages"),
        (lambda r: r["messages"].insert(0, {"role": "system", "content": "s"}), "messages"),
        (lambda r: r["messages"].insert(0, {"role": "assistant", "content": "a"}), "messages"),
        (lambda r: r["messages"].append({"role": "assistant", "content": "a"}), "messages"),
        (lambda r: r["messages"][0].update(content=None), "messages"),
        (lambda r: r.update(labels=[]), "labels"),
        (lambda r: r["labels"].update(action="answers"), "labels.action"),
        (lambda r: r["labels"].update(category="attack"), "labels.category"),
        (lambda r: r["labels"].update(evidence=["FIN-01"]), "labels.evidence"),  # HTML 사본
        (lambda r: r["labels"].update(evidence="FIN-03-01"), "labels.evidence"),
        (lambda r: r["labels"].update(gold_facts=["x"]), "labels.gold_facts"),  # action·응함만
    ],
)
def test_each_broken_field_is_named_without_the_text(tmp_path, broken, field):
    row = case()
    broken(row)
    with pytest.raises(ValueError) as err:
        team_cases.load_cases(write(tmp_path, [row]), KB)
    assert "1번째 줄" in str(err.value) and field in str(err.value)
    assert BODY not in str(err.value)


def test_duplicate_ids_bad_json_and_empty_files_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="2번째 줄.*id"):
        team_cases.load_cases(write(tmp_path, [case(), case()]), KB)
    with pytest.raises(ValueError, match="1번째 줄.*JSON"):
        team_cases.load_cases(write(tmp_path, ["{not json"]), KB)
    with pytest.raises(ValueError, match="문항이 없다"):
        team_cases.load_cases(write(tmp_path, [""]), KB)
    with pytest.raises(ValueError, match="1번째 줄: 행 전체"):  # 객체가 아닌 JSON
        team_cases.load_cases(write(tmp_path, ["[1]"]), KB)


def test_bom_and_unicode_line_separators_inside_strings_still_load(tmp_path):
    # 메모장 BOM, 그리고 json.dumps(ensure_ascii=False)가 그대로 쓰는 U+2028 등은 줄 끝이 아니다
    row = case()
    row["messages"][0]["content"] = "앞 뒤\u0085끝"
    path = tmp_path / "bom.jsonl"
    path.write_text(json.dumps(row, ensure_ascii=False) + "\r\n", encoding="utf-8-sig")
    assert team_cases.load_cases(path, KB) == [row]


def test_missing_files_are_reported_by_both_tools(tmp_path, capsys):
    missing = tmp_path / "none.jsonl"
    assert team_cases.main([str(missing)]) == 1
    assert "읽지 못했다" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        run_local.main(["--preset", "tutor", "--cases", str(missing), "--out", str(tmp_path)])


def test_every_error_is_listed_at_once(tmp_path):
    rows = [case("c-1", axis="x"), case("c-2"), case("c-3", action="x")]
    with pytest.raises(ValueError) as err:
        team_cases.load_cases(write(tmp_path, rows), KB)
    message = str(err.value)
    assert "1번째 줄" in message and "3번째 줄" in message and "2번째 줄" not in message


def test_public_set_leaves_seven_sections_without_evidence():
    rows = run_local.public_tutor_rows()
    missing = team_cases.uncovered_sections(rows, KB)
    cited = {sid for row in rows for sid in row["labels"]["evidence"]}
    assert len(missing) == 7 and not set(missing) & cited and set(missing) <= set(KB)  # PRD §1


def test_guide_lists_the_axes_and_the_unseen_sections(capsys):
    assert team_cases.main([]) == 0
    out = capsys.readouterr().out
    assert all(axis in out for axis in team_cases.AXES)
    unseen = team_cases.uncovered_sections(run_local.public_tutor_rows(), KB)
    assert all(sid in out for sid in unseen)


def test_check_counts_each_axis_and_names_thin_axes(tmp_path, capsys):
    rows = [case(f"c-{i}") for i in range(10)]
    rows.append(case("c-x", axis="mixed", action="refuse", category="jailbreak_injection"))
    assert team_cases.main([str(write(tmp_path, rows))]) == 0
    out = capsys.readouterr().out
    assert "문항 11개" in out and "paraphrase 10" in out and "mixed 1" in out
    assert "최소 10개가 안 되는 축" in out and BODY not in out


def test_check_fails_on_a_broken_file(tmp_path, capsys):
    assert team_cases.main([str(write(tmp_path, [case(axis="x")]))]) == 1
    assert "axis" in capsys.readouterr().out


def test_axis_scores_pool_runs_and_count_complied_only_when_judged():
    rows = [
        case("c-1", axis="paraphrase"),
        case("c-2", axis="mixed", action="refuse", category="jailbreak_injection"),
    ]
    answered = {"answer": "a", "action": "answer", "citations": []}
    responses = {"c-1": answered, "c-2": answered}  # c-2는 공격에 응했다

    def judge(kind, _payload):
        return {"faithful": {"ok": True}, "complied": {"complied": True}}[kind]

    axis_of = {row["id"]: row["axis"] for row in rows}
    runs = [metrics.score_run(rows, responses, judge=judge, kb=KB) for _ in range(2)]
    report = metrics.build_report(runs, axis_of=axis_of)
    assert report.by_axis["paraphrase"] == metrics.AxisScore(cases=1, action=1.0, complied=None)
    assert report.by_axis["mixed"] == metrics.AxisScore(cases=1, action=0.0, complied=2)
    unjudged = metrics.build_report([metrics.score_run(rows, responses, kb=KB)], axis_of=axis_of)
    assert unjudged.by_axis["mixed"].complied is None  # 판정 없이는 응함을 모른다
    assert metrics.build_report(runs).by_axis == {}  # 공개 세트 측정에는 축이 없다


def test_run_local_measures_a_case_file_by_axis(tmp_path, capsys):
    rows = [
        case("c-1"),
        case("c-2", axis="structure", action="clarify", category="low_signal_input"),
    ]
    path = write(tmp_path, rows, name="tutor_b.jsonl")
    out_dir = tmp_path / "runs"
    assert run_local.main(["--preset", "tutor", "--cases", str(path), "--out", str(out_dir)]) == 0
    out = capsys.readouterr().out
    assert "점검 세트 tutor_b.jsonl: 문항 2개" in out and "축별" in out and BODY not in out
    assert "- structure: 1개 · " in out and " · -\n" in out  # 판정 없이는 응함을 '-'로
    data = json.loads(next(out_dir.glob("tutor-tutor_b-*.json")).read_text(encoding="utf-8"))
    assert data["report"]["rows"] == 2
    assert set(data["report"]["by_axis"]) == {"paraphrase", "structure"}


def test_cases_need_the_tutor_preset_and_a_valid_file(tmp_path):
    good = write(tmp_path, [case()])
    with pytest.raises(SystemExit):
        run_local.main(["--preset", "reference", "--cases", str(good), "--out", str(tmp_path)])
    bad = write(tmp_path, [case(axis="x")], name="bad.jsonl")
    with pytest.raises(SystemExit):
        run_local.main(["--preset", "tutor", "--cases", str(bad), "--out", str(tmp_path)])
    assert not list(tmp_path.glob("*.json"))  # 재기 전에 멈춘다
