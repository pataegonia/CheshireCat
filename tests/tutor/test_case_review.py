"""점검 세트 초안 고르기(TU-16·TU-19, #80): input()을 정해 둔 키로 바꿔 시험한다.

문항 내용은 테스트용 자리표시뿐이다(점검 세트 문항은 팀이 쓰거나 nano로 만든다).
"""

import io
from collections import Counter
from types import SimpleNamespace

import pytest

from integration.actions import load_actions
from tutor.eval import case_review as cr
from tutor.eval import case_set, team_cases
from tutor.eval.grader import KB_DIR, score_local

KB = score_local.load_kb_dir(KB_DIR)
CTX = cr.Context(KB, load_actions())


def draft(row_id, axis, category="normal", action="answer", evidence=(), asked=None, by="table"):
    row = {
        "id": row_id,
        "endpoint": "tutor",
        "axis": axis,
        "messages": [{"role": "user", "content": f"질문 자리 {row_id}"}],
        "labels": {"action": action, "category": category, "evidence": list(evidence)},
        "draft": {"form": "꼴 자리", "sections": ["PY-01-01"], "evidence": list(evidence)},
    }
    row["draft"]["category"] = asked
    row["draft"]["action_by"] = by
    return row


def keys(*pressed):
    """정해 둔 키를 차례로 돌려주는 input(). 다 쓰면 EOFError(SSH가 끊긴 것과 같다)."""
    left = iter(pressed)

    def read(_prompt=""):
        try:
            return next(left)
        except StopIteration:
            raise EOFError from None

    return read


@pytest.fixture
def files(monkeypatch, tmp_path):
    paths = SimpleNamespace(
        draft=tmp_path / "team_cases" / "draft.jsonl",
        reviewed=tmp_path / "team_cases" / "reviewed.jsonl",
        final=tmp_path / "team_cases" / "tutor.jsonl",
    )
    monkeypatch.setattr(case_set, "DRAFT_PATH", paths.draft)
    monkeypatch.setattr(case_set, "REVIEWED_PATH", paths.reviewed)
    monkeypatch.setattr(case_set, "FINAL_PATH", paths.final)
    return paths


TWO = cr.Goal(0, 2)  # 축 최소 없이 모두 2개


def run_review(drafts, *pressed, goal=TWO, done=(), kept=None):
    out = io.StringIO()
    counts = cr.review(
        drafts,
        done=set(done),
        kept=Counter(kept or {}),
        goal=goal,
        ctx=CTX,
        read=keys(*pressed),
        out=out,
    )
    return counts, out.getvalue()


def test_keep_drop_and_change_action_until_the_goal(files):
    drafts = [draft("p1", "paraphrase"), draft("p2", "paraphrase"), draft("m1", "mixed")]
    drafts.append(draft("m2", "mixed", "jailbreak_injection", "refuse"))
    # 남긴 수가 적은 축부터: p1 남김 → m1 버림 → m2를 escalate(7)로 바꿔 남김 → 2개라 끝
    counts, out = run_review(drafts, "", "n", "7", "")
    assert counts == {"paraphrase": 1, "mixed": 1} and "목표를 채웠다" in out
    final = case_set.load_cases(files.final, KB)  # 점검 세트 검사를 그대로 통과한다
    assert [r["id"] for r in final] == ["p1", "m2"] and "draft" not in final[0]
    assert final[1]["labels"] == {
        "action": "escalate",
        "category": "jailbreak_injection",
        "evidence": [],
    }
    log = case_set.read_jsonl(files.reviewed)
    assert [(r["id"], r["decision"]) for r in log] == [
        ("p1", "keep"),
        ("m1", "drop"),
        ("m2", "keep"),
    ]
    # 목표를 채워서 더 보이지 않았다(초안 머리 줄로 본다: 화면의 임시 경로에 p2가 들 수 있다)
    assert "] p2 ·" not in out


def test_screen_shows_axis_form_asked_category_dialog_and_labels(files):
    row = draft(
        "h1", "paraphrase", "harmful_content", "redirect", asked="harmful_content", by="nano"
    )
    _counts, out = run_review([row, draft("m1", "mixed")], "q")
    assert "[paraphrase] h1" in out and "꼴: 꼴 자리 · 정해 준 범주 harmful_content" in out
    assert "질문 자리 h1" in out and "1 answer" in out and "7 escalate" in out
    assert "action redirect (nano가 고름) · 표의 action: refuse 또는 redirect" in out
    assert "공개" not in out  # 공개 세트 행은 화면에도 없다
    assert not files.final.exists() and not files.reviewed.exists()  # q는 그 초안을 남기지 않는다
    assert "주의:" not in out  # 정해 준 범주와 같으면 경고가 없다
    _counts, out = run_review([draft("m1", "mixed")], "q")
    assert "꼴: 꼴 자리\n" in out  # 범주를 정해 주지 않은 축


def test_screen_warns_when_the_draft_category_differs_from_the_asked_one(files):
    row = draft("h2", "paraphrase", "normal", "answer", asked="harmful_content")
    _counts, out = run_review([row], "q")
    assert "주의: 정해 준 범주와 초안 범주(normal)가 다르다" in out


def test_screen_warns_when_some_evidence_titles_did_not_match(files):
    # nano가 낸 근거 제목 중 맞지 않은 것은 버려졌다: 두 섹션을 묻는 문항이 근거 하나로 남을 수 있다
    row = draft("n1", "mixed", evidence=["PY-01-01"])
    row["draft"]["unmatched_evidence"] = ["엉뚱한 제목"]
    _counts, out = run_review([row], "q")
    assert "주의: 맞는 섹션이 없어 버린 근거 제목: 엉뚱한 제목" in out
    _counts, out = run_review([draft("n2", "mixed", evidence=["PY-01-01"])], "q")
    assert "버린 근거 제목" not in out


def test_changing_the_category_resets_action_and_evidence_from_the_table(files):
    drafts = [
        draft("u1", "unseen_section", evidence=["PY-01-01"]),
        draft("u2", "unseen_section", "out_of_material", "abstain", evidence=[]),
    ]
    drafts[1]["draft"]["evidence"] = ["PY-01-01"]  # nano가 낸 근거(인용 규칙 때문에 비워 둠)
    # u1: 범주 8(harmful_content)로 → refuse·근거 없음. 잘못된 번호와 빈 입력은 바꾸지 않는다
    # u2: 범주 7(pii_disclosure)로 → answer·근거는 초안 근거
    pressed = ["c", "99", "c", "", "c", "8", "", "ㅊ", "7", ""]
    _counts, out = run_review(drafts, *pressed, goal=cr.Goal(0, 2))
    labels = [r["labels"] for r in case_set.read_jsonl(files.final)]
    assert labels == [
        {"action": "refuse", "category": "harmful_content", "evidence": []},
        {"action": "answer", "category": "pii_disclosure", "evidence": ["PY-01-01"]},
    ]
    assert "범주를 바꾸지 않았다" in out and "범주를 바꿔 표대로" in out


def test_changing_the_action_keeps_evidence_only_where_the_tutor_cites(files):
    # refuse·clarify 등은 튜터가 인용하지 않아, 근거를 남기면 그 행의 인용 점수는 늘 0이다
    drafts = [draft("a1", "premise", evidence=["PY-01-01"]), draft("a2", "premise")]
    _counts, out = run_review(drafts, "6", "2", "", "6", "1", "", goal=TWO)
    labels = [r["labels"] for r in case_set.read_jsonl(files.final)]
    assert labels == [
        {"action": "scaffold", "category": "normal", "evidence": ["PY-01-01"]},  # 6 → 2: 되살림
        {"action": "answer", "category": "normal", "evidence": []},
    ]
    assert "근거가 필요한 문항인데 없어" in out  # a2는 근거가 없어 인용을 재지 않는다고 알린다


def test_resume_skips_reviewed_drafts_and_counts_kept_rows(monkeypatch, files, capsys):
    drafts = [draft("p1", "paraphrase"), draft("m1", "mixed"), draft("p2", "paraphrase")]
    case_set.append_rows(files.draft, drafts)
    case_set.append_rows(files.reviewed, [{"id": "p1", "decision": "drop"}])
    case_set.append_rows(files.final, [case_set.final_row(drafts[1])])  # 기록 전에 끊긴 것
    monkeypatch.setattr("builtins.input", keys("q"))
    assert team_cases.main(["review"]) == 0
    out = capsys.readouterr().out
    assert "[paraphrase] p2 ·" in out and "] p1 ·" not in out and "[mixed]" not in out
    assert "mixed 1" in out and "모두 1개" in out


def test_hangul_keys_unknown_keys_and_eof(files):
    drafts = [draft("p1", "paraphrase"), draft("p2", "paraphrase"), draft("p3", "paraphrase")]
    counts, out = run_review(drafts, "x", "ㅜ", "", goal=cr.Goal(0, 5))  # 셋째에서 입력이 끊긴다
    assert "모르는 키다" in out and counts == {"paraphrase": 1}
    log = case_set.read_jsonl(files.reviewed)
    assert [(r["id"], r["decision"]) for r in log] == [("p1", "drop"), ("p2", "keep")]
    assert "모두 1개" in out  # 끊겨도 요약을 내고 그때까지 고른 것은 남아 있다


def test_running_out_of_drafts_names_the_short_axes(files):
    _counts, out = run_review([draft("p1", "paraphrase")], "", goal=cr.Goal(1, 1))
    assert "고를 초안이 더 없다" in out and "--axis unseen_section" in out
    assert "--axis paraphrase" not in out  # 채운 축은 더 만들 필요가 없다


def test_review_command_reads_the_files_and_uses_input(monkeypatch, files, capsys):
    with pytest.raises(SystemExit):  # 초안이 없으면 generate부터
        team_cases.main(["review"])
    assert "generate --yes" in capsys.readouterr().err
    case_set.append_rows(files.draft, [draft("p1", "paraphrase"), draft("m1", "mixed")])
    monkeypatch.setattr("builtins.input", keys("", ""))
    assert team_cases.main(["review", "--min-per-axis", "0", "--total", "2"]) == 0
    assert [r["id"] for r in case_set.load_cases(files.final, KB)] == ["p1", "m1"]
    assert "목표를 채웠다" in capsys.readouterr().out
    assert team_cases.main(["review", "--min-per-axis", "0", "--total", "2"]) == 0  # 이미 채움
    assert len(case_set.read_jsonl(files.final)) == 2


def test_default_goal_is_ten_per_axis_and_fifty_in_all():
    p = team_cases._parser()
    args = p.parse_args(["review"])
    assert (args.min_per_axis, args.total) == (case_set.MIN_PER_AXIS, case_set.TARGET_TOTAL)
    assert (case_set.MIN_PER_AXIS, case_set.TARGET_TOTAL) == (10, 50)
    assert not cr.Goal(10, 50).reached(Counter({axis: 9 for axis in case_set.AXES}))
    assert cr.Goal(10, 50).reached(Counter({axis: 10 for axis in case_set.AXES}))
    assert not cr.Goal(10, 60).reached(Counter({axis: 10 for axis in case_set.AXES}))
