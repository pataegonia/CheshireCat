"""저장소 설정 그대로(config/tutor.yaml) 띄운 앱의 계약(튜터 동결 설정 11/18, 튜터 #220 대비).

다른 통합 테스트는 동결 토글(`applib.FREEZE_TOGGLES_OFF`)을 끈 기준 설정에서 돈다(`app_env`).
여기서는 저장소 설정을 그대로 읽은 앱(`repo_config_client`)을 본다. 토글은 앱의
`app.state.tutor_cfg.toggles`에서 읽고, `intent_override`가 없으면(동결 설정 전의 main) "off"로
본다.

- 기준 설정 사본은 저장소 설정과 동결 토글 키에서만 다르다.
- 정상 질문 1건: 200 + 튜터 스키마, 가짜 LLM의 답이 나간다. 튜터 LLM 호출 스키마 순서는
  intent_override가 off면 [MainCallOut], serial이면 [IntentOut, MainCallOut], parallel이면 둘이
  한 번씩.
- serial에서 T 이름표가 고정 문장 조합(유해-실행)이면 주 호출 없이 refuse를 낸다.
- T 호출이 실패해도 200 + 스키마이고 주 호출 답이 나간다.
- 공개 튜터 200행: 행마다 200 + 스키마, 요청당 튜터 LLM 호출이 off면 1회 이하, 켜져 있으면 2회 이하.

학습자 발화는 test_real_guard.py의 QUESTION을 다시 쓴다. 공개 세트 행은 test_e2e의 것을 그대로
보낸다.
"""

import json

import pytest
import yaml

from tests.integration.applib import (
    ANSWER_WORD,
    FREEZE_TOGGLES_OFF,
    ROOT,
    assert_tutor_contract,
    user,
)
from tests.integration.test_e2e import (
    HEADERS,
    TUTOR_ROWS,
    assert_tutor_response,
    scorer_payload,
)
from tests.integration.test_real_guard import QUESTION

MAIN, INTENT = "MainCallOut", "IntentOut"


def intent_mode(client) -> str:
    """앱이 읽은 intent_override. 토글이 없으면(동결 설정 전의 main) "off"."""
    return str(getattr(client.app.state.tutor_cfg.toggles, "intent_override", "off"))


def ask(client, fake_send, body: dict) -> tuple[dict, list[str]]:
    """요청 하나를 보내고 (응답 본문, 이번 요청의 튜터 LLM 호출 스키마 이름)을 돌려준다."""
    before = len(fake_send.names)
    r = client.post("/tutor", json=body, headers=HEADERS)
    assert r.status_code == 200, r.status_code
    return r.json(), fake_send.names[before:]


def test_baseline_copy_differs_from_the_repo_config_only_in_freeze_toggles(baseline_tutor_config):
    repo = yaml.safe_load((ROOT / "config" / "tutor.yaml").read_text(encoding="utf-8"))
    copy = yaml.safe_load(baseline_tutor_config.read_text(encoding="utf-8"))
    assert {k: v for k, v in copy.items() if k != "toggles"} == {
        k: v for k, v in repo.items() if k != "toggles"
    }
    rest = {k: v for k, v in repo["toggles"].items() if k not in FREEZE_TOGGLES_OFF}
    assert {k: v for k, v in copy["toggles"].items() if k not in FREEZE_TOGGLES_OFF} == rest
    present = [k for k in FREEZE_TOGGLES_OFF if k in repo["toggles"]]
    assert set(copy["toggles"]) == set(repo["toggles"])  # 저장소에 없는 키는 더하지 않는다
    assert {k: copy["toggles"][k] for k in present} == {k: FREEZE_TOGGLES_OFF[k] for k in present}


def test_normal_question_calls_intent_then_main_as_the_repo_config_says(
    repo_config_client, fake_send
):
    client = repo_config_client
    mode = intent_mode(client)
    body, names = ask(client, fake_send, user(QUESTION))
    seen = f"intent_override={mode}, 호출={names}"
    assert_tutor_contract(body)
    assert ANSWER_WORD in body["answer"], seen  # 가짜 LLM(주 호출)의 답이 실제로 나간다
    if mode == "off":
        assert names == [MAIN], seen
    elif mode == "serial":
        assert names == [INTENT, MAIN], seen
    else:
        assert sorted(names) == sorted([INTENT, MAIN]), seen


def test_serial_intent_label_with_a_fixed_sentence_skips_the_main_call(
    repo_config_client, fake_send
):
    client = repo_config_client
    if intent_mode(client) != "serial":
        pytest.skip(f"intent_override={intent_mode(client)}: serial일 때만 본다")
    fake_send.intent_label = "유해-실행"
    body, names = ask(client, fake_send, user(QUESTION))
    seen = f"호출={names}, action={body.get('action')}"
    assert_tutor_contract(body)
    assert body["action"] == "refuse" and body["citations"] == [], seen
    assert names == [INTENT], seen  # 주 호출 없이 고정 문장


def test_a_failed_intent_call_still_sends_the_main_call_answer(repo_config_client, fake_send):
    client = repo_config_client
    if intent_mode(client) == "off":
        pytest.skip("intent_override=off: T를 부르지 않는다")
    fake_send.intent_fails = True
    body, names = ask(client, fake_send, user(QUESTION))
    seen = f"intent_override={intent_mode(client)}, 호출={names}"
    assert_tutor_contract(body)
    assert ANSWER_WORD in body["answer"], seen
    assert INTENT in names and MAIN in names, seen
    assert fake_send.unknown == []  # T 실패는 모르는 스키마가 아니다


def test_public_tutor_rows_answer_within_the_call_budget(repo_config_client, fake_send):
    client = repo_config_client
    mode = intent_mode(client)
    budget = 1 if mode == "off" else 2
    fake_send.vary = True
    over = []
    for row in TUTOR_ROWS:
        raw = json.dumps(scorer_payload(row), ensure_ascii=False).encode("utf-8")
        before = len(fake_send.names)
        r = client.post("/tutor", content=raw, headers=HEADERS)
        assert r.status_code == 200, row["id"]
        assert_tutor_response(r.json())
        calls = len(fake_send.names) - before
        if calls > budget:
            over.append((row["id"], fake_send.names[before:]))
    assert over == [], f"intent_override={mode}, 요청당 튜터 LLM 상한 {budget}회를 넘은 행: {over}"
