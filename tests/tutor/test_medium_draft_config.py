"""후보 A 설정(config/tutor_medium_draft.yaml)과 여러 초안 서버 로그. LLM은 가짜다(API 0회)."""

import logging

from tests.tutor.test_multi_draft import GOOD_OUT, STREAM_MED, ScriptLLM, ok, result, run_flow
from tutor.runtime import config

CANDIDATE = config.ROOT / "config" / "tutor_medium_draft.yaml"
REPO = config.ROOT / config.DEFAULT_CONFIG


def test_candidate_differs_from_the_repo_config_only_in_the_third_draft():
    base, cand = config.load_config(REPO), config.load_config(CANDIDATE)
    b, c = base.model_dump(), cand.model_dump()
    drafts_b, drafts_c = b["llm"]["multi_draft"]["drafts"], c["llm"]["multi_draft"]["drafts"]
    assert len(drafts_b) == len(drafts_c) == 3
    assert drafts_c[:2] == drafts_b[:2]
    assert drafts_b[2]["effort"] == "low" and not drafts_b[2]["stream"]
    third = drafts_c[2]
    assert (third["effort"], third["stream"], third["max_tokens"]) == ("medium", True, 6000)
    c["llm"]["multi_draft"]["drafts"][2] = drafts_b[2]
    assert c == b  # 나머지 실효 설정(abstain_if_any·top1·T·E·예산 등)은 그대로


def test_candidate_keeps_the_baseline_switches_the_design_fixed():
    md = config.load_config(CANDIDATE).llm.multi_draft
    assert md.enabled and md.abstain_if_any == "off"  # 4dc68e1의 tutor_med.yaml과 다르다
    assert (md.prefer_top1, md.top1_redraft, md.top1_redraft_abstain) == ("on", "on", "on")
    assert (md.wait_s, md.margin_s) == (17, 3)


def test_log_lines_carry_draft_states_and_the_chosen_index_only(caplog):
    llm = ScriptLLM(
        (0.0, ok(GOOD_OUT)), (0.0, ok(GOOD_OUT)), (0.0, result(error="bad_json", finish="length"))
    )
    with caplog.at_level(logging.INFO, logger="guardrail.access"):
        body, _took, _shadow = run_flow(
            llm, **{"llm.multi_draft.drafts": [{"effort": "low"}, {"effort": "low"}, STREAM_MED]}
        )
    assert body["action"] == "answer"
    assert "multi_draft drafts=vvf" in caplog.text
    assert "multi_draft chosen=0" in caplog.text
    assert "/tutor" not in caplog.text  # 요청 수 세기와 섞이지 않는다
    assert GOOD_OUT.sentences[0].text not in caplog.text  # 본문 없음
