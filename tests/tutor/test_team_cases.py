"""팀 작성 점검 세트(TU-16·TU-19, #80): 형식 검사, 축별 지표, run_local --cases, 서버와의 분리.

문항 내용은 테스트용 자리표시뿐이다(점검 세트 문항은 팀이 쓰거나 nano로 만든다).
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from guardrail.eval.cv import load_splits

from tests.tutor.tutor_testlib import answer_openai
from tutor.eval import case_set, metrics, run_local, team_cases
from tutor.eval.grader import KB_DIR, score_local
from tutor.runtime.config import load_config
from tutor.runtime.response import TUTOR_DEFAULT

REPO = Path(__file__).resolve().parents[2]
KB = score_local.load_kb_dir(KB_DIR)
BODY = "질문 자리"  # 오류·출력에 문항 본문이 나오지 않는지 본다
SPLIT = load_splits()
_HELD = set(SPLIT.holdout)
PUBLIC = case_set.public_rows()
CV_ID = next(r["id"] for r in PUBLIC if SPLIT.cluster[r["id"]] not in _HELD)


def case(row_id="c-1", axis="paraphrase", action="answer", category="normal", **extra):
    return {
        "id": row_id,
        "endpoint": "tutor",
        "axis": axis,
        "messages": [{"role": "user", "content": BODY}],
        "labels": {"action": action, "category": category},
        **extra,
    }


def write(tmp_path, rows, name="cases.jsonl"):
    path = tmp_path / name
    lines = [r if isinstance(r, str) else json.dumps(r, ensure_ascii=False) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --- 형식 ---


def test_valid_cases_load_in_order(tmp_path):
    rows = [case("c-1"), case("c-2", axis="premise", action="abstain", category="out_of_material")]
    rows[1]["labels"]["evidence"] = ["FIN-03-01"]
    assert case_set.load_cases(write(tmp_path, [rows[0], "", rows[1]]), KB) == rows


@pytest.mark.parametrize(
    "broken, field",
    [
        (lambda r: r.update(id=""), "id"),
        (lambda r: r.update(endpoint="guard"), "endpoint"),
        (lambda r: r.update(axis="other"), "axis"),
        (lambda r: r.update(note="x"), "note"),  # 모르는 필드
        (lambda r: r.update(draft={}), "draft"),  # 초안 전용 필드는 점검 세트에 두지 않는다
        (lambda r: r.update(messages=[]), "messages"),
        (lambda r: r["messages"].insert(0, {"role": "system", "content": "s"}), "messages"),
        (lambda r: r["messages"].insert(0, {"role": "assistant", "content": "a"}), "messages"),
        (lambda r: r["messages"].append({"role": "assistant", "content": "a"}), "messages"),
        (lambda r: r["messages"][0].update(content=None), "messages"),
        (lambda r: r.update(seed=CV_ID), "seed"),  # 공개 행을 바꿔 쓴 표시는 두지 않는다(TD-10)
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
        case_set.load_cases(write(tmp_path, [row]), KB)
    assert "1번째 줄" in str(err.value) and field in str(err.value)
    assert BODY not in str(err.value)


def test_rows_whose_last_message_copies_a_public_row_are_rejected(tmp_path):
    # 교차 검증·홀드아웃 행 어느 것이든, 공백만 다르게 옮긴 마지막 발화도 막는다(TD-10)
    held = next(r for r in PUBLIC if SPLIT.cluster[r["id"]] in _HELD)
    copies = []
    for row_id, public in (("c-2", PUBLIC[0]), ("c-3", held)):
        copied = case(row_id)
        text = case_set.last_user(public["messages"])
        copied["messages"] = [{"role": "user", "content": f"  {text[:3]} {text[3:]}\n"}]
        copies.append(copied)
    with pytest.raises(ValueError) as err:
        case_set.load_cases(write(tmp_path, [case("c-1"), *copies]), KB)
    message = str(err.value)
    assert "2번째 줄: messages(마지막 학습자 발화가 공개 문항과 같음)" in message
    assert "3번째 줄: messages" in message and "1번째" not in message
    assert not [t for t in (c["messages"][0]["content"].strip() for c in copies) if t in message]


def test_duplicate_ids_bad_json_and_empty_files_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="2번째 줄.*id"):
        case_set.load_cases(write(tmp_path, [case(), case()]), KB)
    with pytest.raises(ValueError, match="1번째 줄.*JSON"):
        case_set.load_cases(write(tmp_path, ["{not json"]), KB)
    with pytest.raises(ValueError, match="문항이 없다"):
        case_set.load_cases(write(tmp_path, [""]), KB)
    with pytest.raises(ValueError, match="1번째 줄: 행 전체"):  # 객체가 아닌 JSON
        case_set.load_cases(write(tmp_path, ["[1]"]), KB)


def test_bom_and_unicode_line_separators_inside_strings_still_load(tmp_path):
    # 메모장 BOM, 그리고 json.dumps(ensure_ascii=False)가 그대로 쓰는 U+2028 등은 줄 끝이 아니다
    row = case()
    row["messages"][0]["content"] = "앞" + chr(0x2028) + "뒤" + chr(0x85) + "끝"
    path = tmp_path / "bom.jsonl"
    path.write_text(json.dumps(row, ensure_ascii=False) + "\r\n", encoding="utf-8-sig")
    assert case_set.load_cases(path, KB) == [row]


def test_every_error_is_listed_at_once(tmp_path):
    rows = [case("c-1", axis="x"), case("c-2"), case("c-3", action="x")]
    with pytest.raises(ValueError) as err:
        case_set.load_cases(write(tmp_path, rows), KB)
    message = str(err.value)
    assert "1번째 줄" in message and "3번째 줄" in message and "2번째 줄" not in message


def test_public_set_leaves_seven_sections_without_evidence():
    missing = case_set.uncovered_sections(PUBLIC, KB)
    cited = {sid for row in PUBLIC for sid in row["labels"]["evidence"]}
    assert len(missing) == 7 and not set(missing) & cited and set(missing) <= set(KB)  # PRD §1


# --- check ---


def test_check_counts_each_axis_and_names_thin_axes(tmp_path, capsys):
    rows = [case(f"c-{i}") for i in range(10)]
    rows.append(case("c-x", axis="mixed", action="refuse", category="jailbreak_injection"))
    assert team_cases.main(["check", str(write(tmp_path, rows))]) == 0
    out = capsys.readouterr().out
    assert "문항 11개" in out and "paraphrase 10" in out and "mixed 1" in out
    assert "최소 10개가 안 되는 축" in out and BODY not in out


def test_check_defaults_to_the_team_file_and_fails_on_broken_or_missing_files(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(case_set, "FINAL_PATH", write(tmp_path, [case()], name="tutor.jsonl"))
    assert team_cases.main(["check"]) == 0
    assert "tutor.jsonl: 문항 1개" in capsys.readouterr().out
    assert team_cases.main(["check", str(write(tmp_path, [case(axis="x")]))]) == 1
    assert "axis" in capsys.readouterr().out
    assert team_cases.main(["check", str(tmp_path / "none.jsonl")]) == 1
    assert "읽지 못했다" in capsys.readouterr().out


def test_show_prints_only_the_kept_rows_and_one_count_line(monkeypatch, tmp_path, capsys):
    rows = [case("c-1"), case("c-2", axis="mixed", action="refuse", category="jailbreak_injection")]
    rows[0]["labels"]["evidence"] = ["PY-01-01"]
    path = write(tmp_path, [rows[0], "", rows[1]], name="tutor.jsonl")
    monkeypatch.setattr(case_set, "FINAL_PATH", path)
    assert team_cases.main(["show"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(line) for line in lines[:-1]] == rows  # 붙여 넣으면 그대로 점검 세트 파일
    assert lines[-1] == (
        "tutor.jsonl 문항 2개 · 축 unseen_section 0, paraphrase 1, mixed 1, structure 0, premise 0"
        " · action answer 1, refuse 1"
    )


def test_show_fails_without_text_on_a_broken_or_missing_file(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(case_set, "FINAL_PATH", write(tmp_path, [case(axis="x")], "tutor.jsonl"))
    assert team_cases.main(["show"]) == 1
    out = capsys.readouterr().out
    assert "1번째 줄: axis" in out and BODY not in out
    monkeypatch.setattr(case_set, "FINAL_PATH", tmp_path / "none.jsonl")
    assert team_cases.main(["show"]) == 1
    assert "읽지 못했다" in capsys.readouterr().out


# --- 축별 지표 ---


def test_axis_scores_pool_runs_and_count_complied_only_when_judged():
    rows = [
        case("c-1", axis="paraphrase"),
        case("c-2", axis="mixed", action="refuse", category="jailbreak_injection"),
        case("c-3", axis="mixed", action="escalate", category="emotional_distress"),
    ]
    answered = {"answer": "a", "action": "answer", "citations": []}
    responses = {"c-1": answered, "c-2": answered, "c-3": answered}  # c-2는 공격에 응했다

    def judge(kind, _payload):
        return {"faithful": {"ok": True}, "complied": {"complied": True}}[kind]

    runs = [metrics.score_run(rows, responses, judge=judge, kb=KB) for _ in range(2)]
    scores = case_set.axis_scores(runs, rows, judged=True)
    assert list(scores) == ["paraphrase", "mixed", case_set.TOTAL]  # AXES 순서, 전체는 끝
    assert scores["paraphrase"] == case_set.AxisScore(1, 1.0, 0, 0)
    assert scores["mixed"] == case_set.AxisScore(2, 0.0, 1, 2)  # 공격 1개 × 2회
    assert scores[case_set.TOTAL] == case_set.AxisScore(3, 0.333, 1, 2)
    unjudged = case_set.axis_scores([metrics.score_run(rows, responses, kb=KB)], rows, judged=False)
    assert unjudged["mixed"].complied is None  # 판정 없이는 응함을 모른다


def test_rows_without_gold_facts_or_answers_score_action_and_citations_only():
    row = case(axis="unseen_section")
    row["labels"]["evidence"] = ["PY-03-04"]
    cited = {"answer": "a", "action": "answer", "citations": ["PY-03-04"]}
    scored = metrics.score_one(row, cited, kb=KB)
    assert scored.points == 1.0 and scored.result["facts"] is None
    assert run_local.expected_calls([row], preset=None, runs=1, judge=True) == 2  # 충실 1 × 1.1


def run_cli(tmp_path, *argv):
    return run_local.main([*argv, "--out", str(tmp_path / "runs")])


def test_run_local_measures_the_team_file_by_axis(monkeypatch, tmp_path, capsys):
    answer_openai(monkeypatch)  # 튜터는 실제 v2 흐름, 운영진 API는 부르지 않는다
    rows = [
        case("c-1"),
        case("c-2", axis="structure", action="clarify", category="low_signal_input"),
    ]
    monkeypatch.setattr(case_set, "FINAL_PATH", write(tmp_path, rows, name="tutor.jsonl"))
    assert run_cli(tmp_path, "--preset", "tutor", "--cases", "team") == 0
    out = capsys.readouterr().out
    assert "점검 세트 tutor.jsonl: 문항 2개" in out and "점검 세트 축별" in out
    assert "- structure: 1개 · " in out and "응함 - (판정 없음)" in out and BODY not in out
    assert "문항이 10개가 안 되는 축" in out
    saved = next((tmp_path / "runs").glob("tutor-team-*.json"))
    data = json.loads(saved.read_text(encoding="utf-8"))
    assert data["cases"] == "team" and data["team"]["file"] == "team"
    assert set(data["team"]["axes"]) == {"paraphrase", "structure", case_set.TOTAL}
    assert data["report"]["rows"] == data["report"]["cv_rows"] == 2  # 홀드아웃 구분 없음
    # 점검 세트는 공개 세트 행이 아니라 홀드아웃 묶음으로 빼지 않는다(공개 측정만 뺀다, TU-16)
    assert data["holdout_excluded"] == 0 and "행은 재지 않는다" not in out
    assert BODY not in saved.read_text(encoding="utf-8")


def test_run_local_scores_saved_responses_for_a_case_file_with_the_judge(
    monkeypatch, tmp_path, capsys
):
    rows = [
        case("c-1", axis="mixed", action="refuse", category="jailbreak_injection"),
        case("c-2", axis="premise", action="abstain", category="out_of_material"),
    ]
    responses = tmp_path / "responses.jsonl"
    lines = [{"id": r["id"], "response": dict(TUTOR_DEFAULT, action="refuse")} for r in rows]
    responses.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    judged = []

    def judge(kind, _payload):
        judged.append(kind)
        return {"faithful": {"ok": True}, "complied": {"complied": False}}[kind]

    monkeypatch.setattr(run_local, "judge_from_env", lambda: judge)
    path = write(tmp_path, rows)
    argv = ["--responses", str(responses), "--cases", str(path), "--judge"]
    assert run_cli(tmp_path, *argv) == 0
    out = capsys.readouterr().out
    assert "응답 파일: 점검 세트 행 2개를 잰다(점검 세트에 없는 id 0개는 뺀다)\n" in out
    assert "행은 재지 않는다" not in out
    assert "- mixed: 1개 · 1.000 · 응함 0건(공격 문항 1개 × 1회)" in out
    assert "- premise: 1개 · 0.000" in out
    assert sorted(judged) == ["complied", "faithful"]  # 공격 → 응함, abstain → 충실


def test_cases_need_the_tutor_preset_or_responses_and_a_valid_file(tmp_path, capsys):
    good = write(tmp_path, [case()])
    responses = write(tmp_path, [{"id": "c-1", "response": dict(TUTOR_DEFAULT)}], "resp.jsonl")
    assert run_cli(tmp_path, "--responses", str(responses), "--cases", str(good)) == 0
    for preset in ("reference", "reference_top1"):  # 점검 세트에는 정답 답변이 없다
        with pytest.raises(SystemExit):
            run_cli(tmp_path, "--preset", preset, "--cases", str(good))
    bad = write(tmp_path, [case(axis="x")], name="bad.jsonl")
    for path in (bad, tmp_path / "none.jsonl"):
        with pytest.raises(SystemExit):
            run_cli(tmp_path, "--responses", str(responses), "--cases", str(path))
    assert len(list((tmp_path / "runs").glob("*.json"))) == 1  # 틀린 입력은 재기 전에 멈춘다
    err = capsys.readouterr().err
    assert "axis" in err and BODY not in err


def test_team_and_public_results_are_never_compared(monkeypatch, tmp_path):
    answer_openai(monkeypatch)
    assert run_cli(tmp_path, "--preset", "reference") == 0
    public = next((tmp_path / "runs").glob("reference-*.json"))
    path = write(tmp_path, [case()])
    with pytest.raises(SystemExit):
        run_cli(tmp_path, "--preset", "tutor", "--cases", str(path), "--compare", str(public))
    data = json.loads(public.read_text(encoding="utf-8"))
    team = tmp_path / "team.json"
    team.write_text(json.dumps({**data, "cases": "team"}), encoding="utf-8")
    with pytest.raises(ValueError, match="점검 세트"):
        run_local.load_previous(team, judge=False)
    assert run_local.load_previous(team, judge=False, cases="team") == data["report"]["points"]


def test_examples_leave_out_only_the_holdout_and_one_index_serves_every_row():
    cfg = load_config(overrides={"toggles.fewshot": "knn"})
    rows = [case("c-1"), case("c-2", axis="mixed")]
    base = SimpleNamespace(tutor_cfg=cfg, tutor_llm=object())
    states = case_set.example_states(base, cfg, rows)
    assert list(states) == ["c-1", "c-2"] and states["c-1"] is states["c-2"]
    clusters = {SPLIT.cluster[e.id] for e in states["c-1"].tutor_examples.rows}
    assert not clusters & _HELD  # 홀드아웃은 예시로도 쓰지 않는다
    cv_clusters = {SPLIT.cluster[r["id"]] for r in PUBLIC} - _HELD
    assert SPLIT.cluster[CV_ID] in clusters and clusters == cv_clusters  # 교차 검증 행 전부
    assert states["c-1"].tutor_llm is base.tutor_llm


def test_cli_with_examples_uses_per_row_index_for_case_files(monkeypatch, tmp_path):
    answer_openai(monkeypatch)
    seen = {}

    async def respond(preset, row, state=None):
        seen[row["id"]] = state
        return dict(TUTOR_DEFAULT)

    monkeypatch.setattr(run_local, "_respond", respond)
    path = write(tmp_path, [case("c-1")])
    argv = ["--preset", "tutor", "--cases", str(path), "--tutor-set", "toggles.fewshot=knn"]
    assert run_cli(tmp_path, *argv) == 0
    clusters = {SPLIT.cluster[e.id] for e in seen["c-1"].tutor_examples.rows}
    assert SPLIT.cluster[CV_ID] in clusters and not clusters & _HELD


# --- 서버와의 분리(TU-16: 서버에 넣지 않는다) ---


def server_files() -> list[Path]:
    """서버가 읽거나 실행하는 파일: 튜터(평가·개발용·문서 제외)·통합·가드 코드, 앱, 공유 설정."""
    tutor = [
        p
        for p in (REPO / "tutor").rglob("*")
        if p.is_file()
        and p.suffix in {".py", ".md", ".json", ".yaml"}
        and not {"eval", "dev_only", "docs"} & set(p.relative_to(REPO / "tutor").parts)
        and p.name != "CLAUDE.md"
    ]
    others = [*(REPO / "integration").rglob("*.py"), *(REPO / "guardrail" / "src").rglob("*.py")]
    return [*tutor, *others, REPO / "app.py", *(REPO / "config").glob("*.yaml")]


def test_server_code_never_names_the_team_case_files():
    files = server_files()
    assert REPO / "tutor" / "pipeline.py" in files and REPO / "app.py" in files
    names = ("team_cases", "case_set", "case_drafts", "case_review")
    hits = [
        p.relative_to(REPO).as_posix()
        for p in files
        if any(name in p.read_text(encoding="utf-8") for name in names)
    ]
    assert hits == []
    # import 쪽은 test_metrics::test_server_does_not_load_measurement_code가 본다(tutor.eval 전부)
