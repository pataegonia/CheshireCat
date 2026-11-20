"""저장소 설정 그대로(config/tutor.yaml) 띄운 앱의 계약(튜터 동결 설정 11/18, 튜터 #220 대비).

다른 통합 테스트는 동결 토글(`applib.FREEZE_TOGGLES_OFF`)을 끈 기준 설정에서 돈다(`app_env`).
여기서는 저장소 설정을 그대로 읽은 앱(`repo_config_client`)을 본다. 토글은 앱의
`app.state.tutor_cfg.toggles`에서 읽고, `intent_override`가 없으면(동결 설정 전의 main) "off"로
본다.

- 기준 설정 사본은 저장소 설정과 동결 토글 키에서만 다르다.
- 저장소 설정에서 켜진 토글은 동결 토글(`FREEZE_TOGGLES_OFF`)이거나 기준 목록(`BASELINE_ON`)에
  있다. 동결 설정에 새 토글이 켜졌는데 기준 설정에서 끄지 않으면 여기서 걸린다.
- 정상 질문 1건: 200 + 튜터 스키마, 가짜 LLM의 답이 나간다. 튜터 LLM 호출 스키마 순서는
  intent_override가 off면 [MainCallOut], serial이면 [IntentOut, MainCallOut], parallel이면 둘이
  한 번씩.
- serial에서 T 이름표가 고정 문장 조합(유해-실행)이면 주 호출 없이 refuse를 낸다.
- T 호출이 실패해도 200 + 스키마이고 주 호출 답이 나간다.
- 공개 튜터 200행: 행마다 200 + 스키마, 요청당 튜터 LLM 호출이 off면 1회 이하, 켜져 있으면 2회 이하.
  그 요청에서 자료 밖 확인 E(OomOut, 튜터 #232 `oom_gate`)를 불렀으면 상한이 1 더 크다.

학습자 발화는 test_real_guard.py의 QUESTION을 다시 쓴다. 공개 세트 행은 test_e2e의 것을 그대로
보낸다.
"""

import json

import pytest
import yaml

from tests.integration.applib import (
    ANSWER_WORD,
    FREEZE_TOGGLES_OFF,
    OOM_GATE,
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
# 동결 설정 전부터 main에서 켜져 있던 토글(기준 설정에서도 켠다)
BASELINE_ON = frozenset({"signals", "cite_top1", "max_citations"})


def is_off(value) -> bool:
    """꺼진 값: false · "off" · 빈 값(0은 꺼진 값으로 보지 않는다)."""
    return value is False or value is None or value == "off"


def intent_mode(client) -> str:
    """앱이 읽은 intent_override. 토글이 없으면(동결 설정 전의 main) "off"."""
    return str(getattr(client.app.state.tutor_cfg.toggles, "intent_override", "off"))


def extra_drafts(client) -> int:
    """주 호출 초안 여러 개(실험 `llm.multi_draft`)를 켰으면 A 밖의 초안 수, 끄거나 없으면 0."""
    md = getattr(client.app.state.tutor_cfg.llm, "multi_draft", None)
    return len(md.drafts) - 1 if md is not None and md.enabled else 0


def ask(client, fake_send, body: dict) -> tuple[dict, list[str]]:
    """요청 하나를 보내고 (응답 본문, 이번 요청의 튜터 LLM 호출 스키마 이름)을 돌려준다."""
    before = len(fake_send.names)
    r = client.post("/tutor", json=body, headers=HEADERS)
    assert r.status_code == 200, r.status_code
    return r.json(), fake_send.names[before:]


def test_baseline_copy_differs_from_the_repo_config_only_in_freeze_toggles(baseline_tutor_config):
    repo = yaml.safe_load((ROOT / "config" / "tutor.yaml").read_text(encoding="utf-8"))
    copy = yaml.safe_load(baseline_tutor_config.read_text(encoding="utf-8"))
    # 실험 브랜치: 기준 사본은 llm.multi_draft.enabled도 끈다(나머지 값은 같다)
    if "multi_draft" in repo["llm"]:
        assert copy["llm"]["multi_draft"]["enabled"] is False
        copy["llm"]["multi_draft"]["enabled"] = repo["llm"]["multi_draft"]["enabled"]
    assert {k: v for k, v in copy.items() if k != "toggles"} == {
        k: v for k, v in repo.items() if k != "toggles"
    }
    rest = {k: v for k, v in repo["toggles"].items() if k not in FREEZE_TOGGLES_OFF}
    assert {k: v for k, v in copy["toggles"].items() if k not in FREEZE_TOGGLES_OFF} == rest
    present = [k for k in FREEZE_TOGGLES_OFF if k in repo["toggles"]]
    assert set(copy["toggles"]) == set(repo["toggles"])  # 저장소에 없는 키는 더하지 않는다
    assert {k: copy["toggles"][k] for k in present} == {k: FREEZE_TOGGLES_OFF[k] for k in present}


def test_repo_config_turns_on_only_freeze_toggles_and_the_baseline_list():
    """저장소 설정의 toggles 가운데 꺼지지 않은 값은 FREEZE_TOGGLES_OFF나 BASELINE_ON에 있어야
    한다(#227 리뷰). 없으면 기준 설정이 그 토글을 켠 채로 다른 통합 테스트를 돌린다."""
    repo = yaml.safe_load((ROOT / "config" / "tutor.yaml").read_text(encoding="utf-8"))
    toggles = repo["toggles"]
    assert BASELINE_ON <= set(toggles), sorted(BASELINE_ON - set(toggles))  # 기준 목록 이름 확인
    stray = {
        k: v
        for k, v in toggles.items()
        if not is_off(v) and k not in FREEZE_TOGGLES_OFF and k not in BASELINE_ON
    }
    assert stray == {}, f"켜졌는데 applib.FREEZE_TOGGLES_OFF에도 기준 목록에도 없는 토글: {stray}"


def redraft_on() -> bool:
    """저장소 설정이 검색 1위로 다시 받기(`toggles.top1_redraft`)를 켰나. 켜면 조건이 맞는 요청에
    주 호출이 한 번 더 나간다(TU-13 예외 5회, 리더 승인 11/20)."""
    repo = yaml.safe_load((ROOT / "config" / "tutor.yaml").read_text(encoding="utf-8"))
    return bool(repo["toggles"].get("top1_redraft"))


def test_normal_question_calls_intent_then_main_as_the_repo_config_says(
    repo_config_client, fake_send
):
    client = repo_config_client
    mode = intent_mode(client)
    body, names = ask(client, fake_send, user(QUESTION))
    seen = f"intent_override={mode}, 호출={names}"
    assert_tutor_contract(body)
    assert ANSWER_WORD in body["answer"], seen  # 가짜 LLM(주 호출)의 답이 실제로 나간다
    mains = [MAIN] * (1 + extra_drafts(client))  # 실험 multi_draft: 주 호출 초안 K개
    if redraft_on() and names.count(MAIN) == len(mains) + 1 and names[-1] == MAIN:
        names = names[:-1]  # 다시 받기가 주 호출을 한 번 더 보냈다
    if mode == "off":
        assert names == mains, seen
    elif mode == "serial":
        assert names == [INTENT, *mains], seen
    else:
        assert sorted(names) == sorted([INTENT, *mains]), seen


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
    drafts = extra_drafts(client)
    base = (1 if mode == "off" else 2) + drafts
    extra = 1 if redraft_on() else 0  # 다시 받기 주 호출 1회(조건이 맞을 때만)
    fake_send.vary = True
    over = []
    for row in TUTOR_ROWS:
        raw = json.dumps(scorer_payload(row), ensure_ascii=False).encode("utf-8")
        before = len(fake_send.names)
        r = client.post("/tutor", content=raw, headers=HEADERS)
        assert r.status_code == 200, row["id"]
        assert_tutor_response(r.json())
        names = fake_send.names[before:]
        budget = base + (OOM_GATE in names) + extra  # E를 부른 요청은 E · T · 주 호출
        if len(names) > budget or names.count(MAIN) > 1 + drafts + extra:
            over.append((row["id"], names))
    assert over == [], (
        f"intent_override={mode}, 요청당 튜터 LLM 상한 {base}회(E면 +1, 다시 받기면 +1)를"
        f" 넘은 행: {over}"
    )


def test_repo_config_with_multi_draft_off_calls_one_main_like_before(
    open_app, monkeypatch, tmp_path, fake_send
):
    """실험 브랜치의 대조군(런박스 끔 측정): 저장소 설정에서 llm.multi_draft.enabled만 끄면 정상
    질문의 튜터 LLM 호출이 동결 설정 그대로다(serial이면 [IntentOut, MainCallOut])."""
    raw = yaml.safe_load((ROOT / "config" / "tutor.yaml").read_text(encoding="utf-8"))
    if "multi_draft" not in raw["llm"]:
        pytest.skip("multi_draft 키가 없는 설정")
    raw["llm"]["multi_draft"]["enabled"] = False
    path = tmp_path / "tutor-multi-off.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("TUTOR_CONFIG", str(path))
    with open_app() as client:
        mode = intent_mode(client)
        body, names = ask(client, fake_send, user(QUESTION))
    assert_tutor_contract(body)
    expected = {"off": [MAIN], "serial": [INTENT, MAIN]}.get(mode, sorted([INTENT, MAIN]))
    assert (names if mode in ("off", "serial") else sorted(names)) == expected, names
