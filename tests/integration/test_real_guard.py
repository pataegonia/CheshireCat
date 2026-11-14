"""진짜 판정기를 켠 루트 앱(I1.10).

가드 기본 판정기(`guardrail.server.default_factory`)는 OPENAI_API_KEY가 있으면 진짜 판정기
(`Guard.build`), 없으면 상수 판정기다. 다른 통합 테스트는 `app_env`가 키를 지워 상수 판정기로 돈다.
여기서는 `real_guard_client`(가짜 키 + 가드 · 튜터 전송 가짜)로 진짜 판정기 + 튜터를 합친 앱을
띄운다.

- 기동: 진짜 판정기로 뜨고 probe를 마쳤고, 판정 호출은 아직 없다.
- `/guard`: 후보 답변이 없으면 nano 1회(입력 판정), 있으면 2회(+ 출력 판정).
- `/tutor` → 가드: 가드에 classify · judge_output이 있으면 부르고, 없으면 건너뛴 수만 센다.
- 요청 내용 · 판정 글이 앱 상태 · 모듈 · 로그에 남지 않는다.
- 요청마다 가드 nano 호출이 `applib.MAX_GUARD_LLM_CALLS_PER_REQUEST` 안이다.

"""

from tests.integration.applib import (
    ANSWER_WORD,
    AUTH,
    GUARD_BUDGET_HINT,
    GUARD_WORD,
    MAX_GUARD_LLM_CALLS_PER_REQUEST,
    as_bytes,
    assert_guard_contract,
    assert_tutor_contract,
    cache_sizes,
    find_text,
    state_roots,
    user,
)

MARKER = "진짜가드표식RG4"
QUESTION = "리스트에 값을 넣는 방법이 뭐야?"
CANDIDATE = "append를 쓰면 돼요."


def post(client, path: str, body: dict):
    return client.post(path, content=as_bytes(body), headers=AUTH, follow_redirects=False)


def guard_body(content: str, candidate: str | None = None) -> dict:
    body = user(content)
    if candidate is not None:
        body["candidate_answer"] = candidate
    return body


def sent(client, fake_guard_send, path: str, body: dict):
    """요청 하나를 보내고 (응답, 늘어난 가드 nano 호출 수)를 돌려준다. 늘어난 수는 상한 안이다."""
    before = fake_guard_send.count
    r = post(client, path, body)
    assert r.status_code == 200
    calls = fake_guard_send.count - before
    assert calls <= MAX_GUARD_LLM_CALLS_PER_REQUEST[path], (path, calls, GUARD_BUDGET_HINT)
    return r, calls


def test_root_app_starts_with_the_real_judge(real_guard_client, fake_guard_send, caplog):
    # 가드 LLM은 가짜 클라이언트 하나로 probe까지 마쳤고, 판정 호출은 아직 없다
    assert fake_guard_send.clients == 1 and fake_guard_send.probes >= 1
    assert fake_guard_send.count == 0
    started = [r.getMessage() for r in caplog.get_records("setup")]
    assert any("guard judge=real" in line for line in started), started
    # probe가 실패했으면 "guard judge=real probe=<오류 코드>"가 남는다
    assert not any("probe=" in line for line in started), started


def test_guard_calls_nano_once_per_judge(real_guard_client, fake_send, fake_guard_send):
    client = real_guard_client
    # 입력 판정 1회
    g1, calls = sent(client, fake_guard_send, "/guard", guard_body(QUESTION))
    assert calls == 1
    assert_guard_contract(g1.json(), has_candidate=False)
    # 후보 답변이 있으면 출력 판정 1회 더
    g2, calls = sent(client, fake_guard_send, "/guard", guard_body(QUESTION, candidate=CANDIDATE))
    assert calls == 2
    assert_guard_contract(g2.json(), has_candidate=True)
    assert fake_send.count == 0  # 가드는 튜터 전송을 쓰지 않는다


def test_tutor_hands_its_judges_to_the_real_guard(real_guard_client, fake_guard_send):
    app = real_guard_client.app
    guard = app.state.guard
    t, calls = sent(real_guard_client, fake_guard_send, "/tutor", user(QUESTION))
    assert_tutor_contract(t.json())
    assert t.json()["answer"].startswith(ANSWER_WORD)
    skips = dict(app.state.guard_skips)
    # 튜터는 가드의 classify · judge_output을 부른다. 지금 main의 Guard에는 둘이 없다(이슈 #119).
    # 가드가 올리면 이 테스트는 고치지 않아도 위쪽 분기로 넘어간다
    if hasattr(guard, "classify") and hasattr(guard, "judge_output"):
        assert calls >= 1
        assert not [key for key in skips if key[1] in ("missing", "error")], skips
    else:  # 지금 main: 튜터가 두 단계를 건너뛰고 사유만 센다
        assert calls == 0
        assert skips == {("classify", "missing"): 1, ("judge", "missing"): 1}


# 표식이 든 요청들: 후보 답변이 있는 /guard, /tutor
MARKED_SENDS = [
    ("/guard", guard_body(f"{MARKER} {QUESTION}", candidate=f"{MARKER} {CANDIDATE}")),
    ("/tutor", user(f"{MARKER} {QUESTION}")),
]


def test_request_content_is_not_kept_with_the_real_judge(
    real_guard_client, fake_guard_send, caplog
):
    client, app = real_guard_client, real_guard_client.app
    needles = (MARKER, GUARD_WORD + " ", ANSWER_WORD + " ")
    assert find_text(state_roots(app), needles) == []
    caches_before = cache_sizes()
    texts = []
    for path, body in MARKED_SENDS:
        r, _ = sent(client, fake_guard_send, path, body)
        texts.append(r.text)
        # 요청마다 본다(뒤 요청이 앞 요청의 흔적을 덮어쓴 경우를 놓치지 않게)
        assert find_text(state_roots(app), needles) == [], path
        assert cache_sizes() == caches_before, path
    assert GUARD_WORD not in "".join(texts)  # 판정 글은 응답에 나오지 않는다
    # 판정 글과 요청 문구는 로그(DEBUG까지)에 나오지 않는다
    logged = "\n".join(rec.getMessage() for rec in caplog.records) + caplog.text
    assert MARKER not in logged and GUARD_WORD not in logged


def test_real_judge_calls_stay_within_the_budget(real_guard_client, fake_guard_send):
    client = real_guard_client
    for path, body in [("/guard", guard_body(QUESTION)), *MARKED_SENDS]:
        _, calls = sent(client, fake_guard_send, path, body)
        assert calls <= MAX_GUARD_LLM_CALLS_PER_REQUEST[path], (path, calls, GUARD_BUDGET_HINT)
    assert fake_guard_send.count >= 3  # 진짜 판정기가 실제로 nano를 불렀다(/guard 1 + 2)
