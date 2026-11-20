"""초안별 인용 기록·인용 규칙(`llm.multi_draft.cite_rule`)·오프라인 재생. LLM은 가짜다(API 0회).

규칙은 응답이 고른 초안의 assemble 경로에서 그대로 나갈 때 인용 목록만 바꾼다(답·action 그대로).
"""

import json

import pytest

from tests.tutor.test_multi_draft import ScriptLLM, ok, result, run_flow
from tutor.eval import cite_replay, run_local
from tutor.judge import multi_draft, oom_gate
from tutor.judge.main_call import EvidenceOut, MainCallOut, SentenceOut
from tutor.judge.multi_draft import compose_cites
from tutor.judge.oom_gate import OomOut
from tutor.runtime.config import load_config

TOP, OTHER = "PY-01-01", "PY-02-03"  # 시험 질문의 검색 1위 / 근거 묶음 안의 다른 섹션
LOW3 = {"llm.multi_draft.drafts": [{"effort": "low"}] * 3}
RULE = "llm.multi_draft.cite_rule"


def answer(sid, quote, text):
    ev = EvidenceOut(sid=sid, quote=quote)
    return MainCallOut(
        category="normal",
        action="answer",
        sentences=[SentenceOut(text=text, kind="claim", evidence=[ev])],
    )


ONE = answer(TOP, "3회까지의 점수를 기록해요", "야구 점수판 예제는 3회까지의 점수를 기록해요.")
TWO = answer(OTHER, "간격에 0은 쓸 수 없어요", "range의 간격에 0은 쓸 수 없어요.")


def flow(outs, rule="pick", **over):
    llm = ScriptLLM(*[(0.0, ok(o) if o is not None else result(error="timeout")) for o in outs])
    body, _took, shadow = run_flow(llm, **{**LOW3, RULE: rule, **over})
    return body, shadow


# --- 규칙(순수 함수) ---

ANS3 = ["answer"] * 3


def test_pick_is_unchanged():
    assert compose_cites("pick", ["B", "A"], [["A", "B"], ["C"], ["C"]], ANS3, 0) == ["B", "A"]


def test_vote_keeps_sections_cited_by_two_same_action_drafts():
    md = [["A", "X"], ["A"], ["A", "Y"]]
    assert compose_cites("vote", ["X", "A"], md, ANS3, 0) == ["A"]
    # 고른 초안이 인용하지 않은 섹션도 두 초안이 인용하면 들어간다
    assert compose_cites("vote", ["X"], [["X"], ["Y"], ["Y"]], ANS3, 0) == ["Y"]


def test_vote_falls_back_to_pick_when_nothing_has_two_votes():
    assert compose_cites("vote", ["X"], [["X"], ["Y"], ["Z"]], ANS3, 0) == ["X"]


def test_other_action_and_invalid_drafts_do_not_vote():
    actions = ["answer", "abstain", None, "answer"]
    md = [["X"], ["Y"], None, ["Z"]]
    assert compose_cites("vote", ["X"], md, actions, 0) == ["X"]
    md2 = [["X"], ["Y"], None, ["Y"]]  # abstain 초안의 Y는 세지 않는다 → Y 1표
    assert compose_cites("vote", ["X"], md2, actions, 0) == ["X"]
    md3 = [["X"], ["X"], None, ["Z"]]  # abstain 초안의 X도 세지 않는다
    assert compose_cites("pick_drop_lone", ["X"], md3, actions, 0) == ["X"]
    assert compose_cites("pick_add_vote", ["X"], md3, actions, 0) == ["X"]


def test_pick_drop_lone_drops_unshared_sections_but_never_to_empty():
    md = [["A", "X"], ["A"], ["A"]]
    assert compose_cites("pick_drop_lone", ["X", "A"], md, ANS3, 0) == ["A"]
    assert compose_cites("pick_drop_lone", ["X"], [["X"], ["Y"], ["Z"]], ANS3, 0) == ["X"]


def test_pick_add_vote_adds_voted_sections_after_pick():
    md = [["X"], ["Y"], ["Y"]]
    assert compose_cites("pick_add_vote", ["X"], md, ANS3, 0) == ["X", "Y"]


def test_empty_pick_is_left_empty_and_unknown_rule_raises():
    for rule in multi_draft.CITE_RULES:
        assert compose_cites(rule, [], [[], ["Y"], ["Y"]], ANS3, 0) == []
    with pytest.raises(ValueError):
        compose_cites("majority", ["X"], [["X"]], ["answer"], 0)


# --- 흐름: 기록과 규칙 ---


def test_pick_records_per_draft_cites_and_keeps_the_response():
    body, shadow = flow([ONE, TWO, TWO])
    assert shadow.chosen == 0 and body["citations"] == [TOP]
    # 다른 초안은 OTHER를 인용하고 cite_top1이 1위(TOP)를 뒤에 더한다
    assert shadow.md_cites == [[TOP], sorted([TOP, OTHER]), sorted([TOP, OTHER])]
    assert shadow.cite_path is True
    assert shadow.md_cites[shadow.chosen] == sorted(body["citations"])
    assert "PY-" not in shadow.log_field()  # 섹션 ID는 로그 칸에 내지 않는다


def test_invalid_draft_has_no_cites():
    _body, shadow = flow([ONE, None, TWO])
    assert shadow.md_cites[1] is None and shadow.md_cites[2] == sorted([TOP, OTHER])


@pytest.mark.parametrize(
    ("outs", "rule", "cites"),
    [
        ([ONE, TWO, TWO], "vote", {TOP, OTHER}),
        ([ONE, TWO, TWO], "pick_add_vote", {TOP, OTHER}),
        ([ONE, TWO, TWO], "pick_drop_lone", {TOP}),
        ([TWO, ONE, ONE], "vote", {TOP}),
        ([TWO, ONE, ONE], "pick_drop_lone", {TOP}),
        ([TWO, ONE, ONE], "pick_add_vote", {TOP, OTHER}),
    ],
)
def test_rules_change_only_the_citations(outs, rule, cites):
    base, _ = flow(outs)
    body, shadow = flow(outs, rule)
    assert set(body["citations"]) == cites
    assert body["answer"] == base["answer"] and body["action"] == base["action"]
    assert shadow.cite_path is True


def test_pick_order_is_kept_and_quote_append_is_not_affected():
    on = {"toggles.quote_append": True, "toggles.quote_from_top1": True}
    base, _ = flow([TWO, ONE, ONE], **on)
    body, _ = flow([TWO, ONE, ONE], "vote", **on)
    assert base["citations"] == [OTHER, TOP] and body["citations"] == [TOP]
    assert body["answer"] == base["answer"]  # 덧붙인 자료 문장은 pick 인용으로 고른 그대로


def test_e_replaced_response_keeps_its_citations():
    class ByRole(ScriptLLM):
        async def complete(self, role, messages, schema, **kw):
            if role == oom_gate.ROLE:
                return result(parsed=OomOut(asked="a", quote="", found=False))
            return await super().complete(role, messages, schema, **kw)

    llm = ByRole((0.0, ok(ONE)), (0.0, ok(TWO)), (0.0, ok(TWO)))
    body, _took, shadow = run_flow(
        llm,
        **{**LOW3, RULE: "vote", "toggles.oom_gate": "all", "toggles.oom_schedule": "with_request"},
    )
    assert body["action"] == "abstain" and body["citations"] == []
    assert shadow.cite_path is False and shadow.md_cites is not None


def test_fixed_sentence_picked_by_the_main_call_bypasses_the_rule():
    jb = MainCallOut(category="jailbreak_injection", action="refuse", sentences=[])
    body, shadow = flow([jb, jb, ONE], "pick_add_vote")
    assert body["action"] == "refuse" and body["citations"] == []
    assert shadow.md_cites is None and shadow.cite_path is False


def test_fallback_fixed_sentence_bypasses_the_rule():
    # 주 호출 실패 → 기본값(고정 문장): assemble 경로가 아니라 초안 인용 기록도 규칙도 없다
    body, shadow = flow([None, None, None], "pick_add_vote")
    assert body["citations"] == [] and shadow.md_cites is None and shadow.cite_path is False


def test_apply_cite_rule_needs_the_pick_body_itself():
    from tests.tutor.test_flow import state_with
    from tutor.judge.flow import apply_cite_rule

    state = state_with(None, **{RULE: "vote"})
    body = {"answer": "a", "action": "answer", "citations": ["X"]}
    seen = {"md": ([["X"], ["Y"], ["Y"]], ANS3, 0), "pick_body": body}
    assert apply_cite_rule(state, seen, body, dict(body))["citations"] == ["Y"]
    other = dict(body)  # 다른 단계가 바꾼 응답(같은 내용이어도 고른 초안의 본문이 아니다)
    assert apply_cite_rule(state, seen, other, other) is other


# --- 설정 ---


def test_cite_rule_config_accepts_the_rules_and_rejects_others():
    for rule in multi_draft.CITE_RULES:
        assert load_config(overrides={RULE: rule}).llm.multi_draft.cite_rule == rule
    for bad in ("majority", True, None):
        with pytest.raises(ValueError):
            load_config(overrides={RULE: bad})


# --- 행별 기록 · 재생 ---


def test_run_local_rows_have_cites_and_draft_cites(tmp_path, monkeypatch):
    from tests.tutor import tutor_testlib

    tutor_testlib.answer_openai(monkeypatch)
    rows = tmp_path / "rows.jsonl"
    argv = ["--preset", "tutor", "--out", str(tmp_path), "--rows-jsonl", str(rows)]
    argv += ["--tutor-set", "llm.multi_draft.enabled=true"]
    assert run_local.main(argv) == 0
    recs = [json.loads(x) for x in rows.read_text(encoding="utf-8").splitlines()]
    assert all("cites" in r and "md_cites" in r and "md_cite_path" in r for r in recs)
    path = [r for r in recs if r["md_cite_path"]]
    assert path
    for r in path:
        assert set(r["md_cites"][r["md_pick"]]) == set(r["cites"])
        assert all(c is None or c == sorted(c) for c in r["md_cites"])
    # 재생이 같은 파일을 읽고 pick이 기록과 같다
    res = cite_replay.replay_run(recs, cite_replay.gold_rows(), _kb())
    assert res["mismatch_pick_vs_recorded"] == 0 and res["mismatch_recorded_f1"] == 0


def _kb():
    from tutor.eval.grader import KB_DIR, score_local

    return score_local.load_kb_dir(KB_DIR)


def _gold_row(gold):
    return next(
        r
        for r in gold.values()
        if len(r["labels"].get("evidence") or []) == 1 and r["labels"]["action"] == "answer"
    )


def test_replay_on_a_tiny_synthetic_file(tmp_path):
    gold = cite_replay.gold_rows()
    row = _gold_row(gold)
    e = row["labels"]["evidence"][0]
    x = next(s for s in ("PY-01-01", "PR-01-01", "FIN-01-01") if s != e)
    held = sorted(run_local.holdout_ids())[0]
    assert held not in gold  # 홀드아웃 행은 id로 먼저 뺀다
    base = {"run": 0, "action": "answer", "action_ok": 1.0, "complied": None, "points": 0.5}
    path_rec = {
        **base,
        "id": row["id"],
        "cites": [x, e],
        "citation_f1": 2 / 3,
        "md_cites": [sorted([x, e]), [e], [e]],
        "md_actions": ["answer"] * 3,
        "md_pick": 0,
        "md_cite_path": True,
    }
    off_path = {**path_rec, "md_cite_path": False}  # T·E가 바꾼 응답: 규칙 적용 없음
    held_rec = {**path_rec, "id": held}
    rows = tmp_path / "rows.jsonl"
    rows.write_text("".join(json.dumps(r) + "\n" for r in (path_rec, off_path, held_rec)))
    res = cite_replay.replay_run(cite_replay.load_runs([rows])[0][1], gold, _kb())
    assert res["scored_rows"] == 2 and res["cited_rows"] == 2 and res["rule_rows"] == 1
    f1 = res["mean_f1"]
    assert f1["recorded"] == pytest.approx(2 / 3) and f1["pick"] == pytest.approx(2 / 3)
    assert f1["vote"] == pytest.approx((1 + 2 / 3) / 2)
    assert f1["pick_drop_lone"] == pytest.approx((1 + 2 / 3) / 2)
    assert f1["pick_add_vote"] == pytest.approx(2 / 3)
    from tutor.eval.grader import score_local, scoring

    item, variant = score_local.as_scoring_input(row)
    n = 2 + bool(variant["gold_facts"]) + (item["labels"]["action"] in scoring.CONTENT_ACTIONS)
    assert res["delta_tutor"]["vote"] == pytest.approx(100 * (1 - 2 / 3) / n / 2)
    assert res["delta_tutor"]["pick_add_vote"] == 0
    assert res["fixed"]["vote"] == [row["id"]] and res["broken"]["vote"] == []
    assert res["pick_loss_rows"] == {"p_only": 2, "r_only": 0, "both": 0}
    assert res["mismatch_pick_vs_recorded"] == 0 and res["mismatch_recorded_f1"] == 0
    out = tmp_path / "out.txt"
    with out.open("w", encoding="utf-8") as fh:
        assert cite_replay.main([str(rows), str(rows)], out=fh) == 0
    text = out.read_text(encoding="utf-8")
    assert "합침" in text and "vote" in text
