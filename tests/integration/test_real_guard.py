"""진짜 판정기를 켠 루트 앱(I1.10).

가드 기본 판정기(`guardrail.server.default_factory`)는 OPENAI_API_KEY가 있으면 진짜 판정기
(`Guard.build`), 없으면 상수 판정기다. 다른 통합 테스트는 `app_env`가 키를 지워 상수 판정기로 돈다.
여기서는 `real_guard_client`(가짜 키 · 닫힌 주소 + 가드 · 튜터 전송 가짜)로 진짜 판정기 + 튜터를
합친 앱을 띄운다.

- 기동: 진짜 판정기로 뜨고 probe를 마쳤고, 판정 호출은 아직 없다.
- `/guard`: 후보 답변이 없으면 nano 1회(입력 판정), 있으면 2회(+ 출력 판정). 1차 입력 판정이
  탈옥이고 가드 설정에 탈옥 2차 판정(`toggles.jb_check`, 가드 #183)이 켜져 있으면 1회 더.
  2차가 study면 응답 input이 allow · normal, 꺼져 있거나 apply면 block · jailbreak_injection.
- `/guard`: 1차 입력 판정이 normal이고 탈옥 2차를 부르지 않았으며 가드 설정에 자료 밖 확인
  (`toggles.oom_check`, 가드 #215)이 켜져 있으면 1회 더(OomCheckOut). verdict가 not_stated면
  응답 input이 allow · out_of_material, not_asked · stated면 allow · normal. 탈옥 2차와 한 요청에서
  같이 부르지 않는다. 토글이 없는 main에서는 0회다. 스키마 이름은 문자열로만 쓴다.
- `/tutor`에서 classify가 탈옥이면: 2차 꺼짐 1회, 켜짐 + study 3회, 켜짐 + apply 2회.
- `/tutor` → 가드: 가드에 classify · judge_output이 있으면 튜터가 각각 정확히 1회 부르고 건너뛴
  단계가 없다(`assert_tutor_uses_guard`). 없으면 건너뛴 수만 센다. 연결을 보는 것이라
  `real_guard_client`는 튜터의 두 단계를 켠 설정으로 띄운다(`tutor_guard_stages_on`).
- 배포 설정(config/tutor.yaml)은 두 단계를 끈다(11/14 리더 결정, 가드 #170 뒤 런박스 켬/끔 비교
  전까지). 그 설정의 앱은 가드에 두 함수가 있어도 부르지 않는다.
- 요청 내용 · 판정 글이 앱 상태 · 모듈 · 로그 · stdout · stderr에 남지 않는다.
- 요청마다 가드 nano 호출이 상한(`applib.guard_call_budget(real_guard=True)`) 안이다.
"""

import collections

import pytest
from guardrail.schema import OutputVerdict

from tests.integration.applib import (
    ANSWER_WORD,
    AUTH,
    GUARD_BUDGET_HINT,
    GUARD_WORD,
    as_bytes,
    assert_guard_contract,
    assert_tutor_contract,
    cache_sizes,
    find_text,
    guard_calls_per_request,
    state_roots,
    user,
    write_tutor_config,
)

MARKER = "진짜가드표식RG4"
QUESTION = "리스트에 값을 넣는 방법이 뭐야?"
CANDIDATE = "append를 쓰면 돼요."
TUTOR_GUARD_METHODS = ("classify", "judge_output")
TUTOR_GUARD_HINT = (
    "가드 PR에서 이 실패가 나면: 튜터는 classify(messages), "
    "judge_output(messages, candidate_answer, category)를 부른다(guardrail/docs/interface.md). "
    "건너뜀 사유는 tutor/judge/guard_merge.py의 Reason을 본다. "
    "통합 테스트 쪽 수정이 필요하면 통합 담당(pyr27)을 부른다"
)


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
    limit = guard_calls_per_request(path, real_guard=True)
    assert calls <= limit, (path, calls, limit, GUARD_BUDGET_HINT)
    return r, calls


def assert_tutor_uses_guard(client, fake_guard_send, monkeypatch) -> None:
    """`/tutor` 일반 질문 1건에서 튜터가 가드의 classify · judge_output을 각각 정확히 1회 부르고,
    건너뛴 단계(사유 무관)가 없고, 가드 nano 호출이 `/tutor` 상한 안인지 본다.

    두 메서드는 부른 횟수만 세는 감싸개로 바꾼다(원래 메서드를 그대로 부른다). 가드가 규칙만으로
    판정해 nano를 부르지 않을 수 있어 nano 호출의 하한은 두지 않는다. 앱은 튜터의 두 단계를 켠
    설정으로 띄운다(배포 설정은 끔, `tutor_guard_stages_on`).
    """
    app = client.app
    guard = app.state.guard
    called: collections.Counter[str] = collections.Counter()
    for name in TUTOR_GUARD_METHODS:
        original = getattr(guard, name)

        async def counted(*args, _original=original, _name=name, **kwargs):
            called[_name] += 1
            return await _original(*args, **kwargs)

        monkeypatch.setattr(guard, name, counted)
    skips_before = collections.Counter(app.state.guard_skips)
    before = fake_guard_send.count
    r = post(client, "/tutor", user(QUESTION))
    calls = fake_guard_send.count - before
    skipped = dict(collections.Counter(app.state.guard_skips) - skips_before)
    toggles = app.state.tutor_cfg.toggles
    seen = f"감싸개 횟수 {dict(called)}, guard_skips {skipped}, 가드 nano {calls}회"
    seen += f"(튜터 설정 guard_classify={toggles.guard_classify}, verify={toggles.verify}). "
    seen += TUTOR_GUARD_HINT
    assert r.status_code == 200, seen
    assert_tutor_contract(r.json())
    assert called == {"classify": 1, "judge_output": 1}, seen
    assert skipped == {}, seen  # timeout · error · invalid 등 어떤 사유로든 건너뛰지 않았다
    assert calls <= guard_calls_per_request("/tutor", real_guard=True), seen


def test_root_app_starts_with_the_real_judge(real_guard_client, fake_guard_send, caplog):
    # 가드 LLM은 가짜 클라이언트 하나로 probe까지 마쳤고, 판정 호출은 아직 없다
    assert fake_guard_send.clients == 1 and fake_guard_send.probes >= 1
    assert fake_guard_send.count == 0
    started = [r.getMessage() for r in caplog.get_records("setup")]
    assert any("guard judge=real" in line for line in started), started
    # probe가 실패했으면 "guard judge=real probe=<오류 코드>"가 남는다
    assert not any("probe=" in line for line in started), started


def oom_check_on(client) -> bool:
    """가드 설정에 자료 밖 확인이 켜져 있는가. 가드 #215 전의 main에는 이 토글이 없다(False)."""
    return bool(getattr(client.app.state.cfg.toggles, "oom_check", False))


def toggles_seen(client, fake_guard_send, before: collections.Counter) -> str:
    """실패 메시지: 토글 상태와 이번 요청의 스키마별 호출 수(가드 PR에서 무엇이 달라졌는지)."""
    delta = dict(collections.Counter(fake_guard_send.by_schema) - before)
    state = f"oom_check={oom_check_on(client)}, jb_check={jb_check_on(client)}"
    return f"{state}, 이번 요청 by_schema={delta}"


def test_guard_calls_nano_once_per_judge_plus_the_oom_check_when_it_is_on(
    real_guard_client, fake_send, fake_guard_send
):
    """일반 질문의 `/guard`: 입력 판정 1회(+ 후보 답변이 있으면 출력 판정 1회). 자료 밖 확인이 켜져
    있으면 요청마다 OomCheckOut 1회가 더해진다(가드 #215, main은 토글이 없어 0회)."""
    client = real_guard_client
    oom = int(oom_check_on(client))
    for candidate, judges in ((None, 1), (CANDIDATE, 2)):
        before = collections.Counter(fake_guard_send.by_schema)
        r, calls = sent(
            client, fake_guard_send, "/guard", guard_body(QUESTION, candidate=candidate)
        )
        seen = f"{toggles_seen(client, fake_guard_send, before)}, 이번 요청 {calls}회"
        assert calls == judges + oom, seen
        assert fake_guard_send.by_schema["OomCheckOut"] - before["OomCheckOut"] == oom, seen
        assert_guard_contract(r.json(), has_candidate=candidate is not None)
    assert fake_send.count == 0  # 가드는 튜터 전송을 쓰지 않는다


JAILBREAK = {"category": "jailbreak_injection"}  # 가짜가 1차 입력 판정을 탈옥으로 내게 한다


def jb_check_on(client) -> bool:
    """가드 설정에 탈옥 2차 판정이 켜져 있는가. 가드 #183 전의 main에는 이 토글이 없다(False)."""
    return bool(getattr(client.app.state.cfg.toggles, "jb_check", False))


BLOCKED_JAILBREAK = {"decision": "block", "category": "jailbreak_injection"}
ALLOWED_NORMAL = {"decision": "allow", "category": "normal"}


def expected_input(jb: bool, verdict: str) -> dict:
    """1차가 탈옥일 때 `/guard` 응답의 input. 2차가 꺼져 있거나 apply면 탈옥으로 막고, study(언급일
    뿐)면 일반으로 바꿔 통과시킨다(가드 #183)."""
    return ALLOWED_NORMAL if jb and verdict == "study" else BLOCKED_JAILBREAK


def test_first_judge_jailbreak_calls_the_second_check_when_it_is_on(
    real_guard_client, fake_guard_send
):
    """1차 입력 판정이 탈옥인 경로. 탈옥 2차 판정이 켜져 있으면 요청마다 nano 1회(JbCheckOut)가
    더해지고, 2차 판정에 따라 응답의 input이 정해진다. #183 전의 main(토글 없음)과 #183을 합친
    상태 둘 다에서 통과한다. main에서도 input을 보므로 가짜가 force를 무시하면 여기서 걸린다."""
    client = real_guard_client
    jb = jb_check_on(client)
    # verdict를 걸지 않으면 가짜 기본값(study). 2차가 켜져 있을 때만 apply · study를 걸어 본다
    verdicts = (None, "apply", "study") if jb else (None,)
    counts: collections.Counter[tuple[str, bool]] = collections.Counter()
    for verdict in verdicts:
        force = dict(JAILBREAK) if verdict is None else {**JAILBREAK, "verdict": verdict}
        used = verdict or "study"  # 가짜 기본값
        fake_guard_send.force = force
        for candidate, judges in ((None, 1), (CANDIDATE, 2)):
            jb_before = fake_guard_send.by_schema["JbCheckOut"]
            before = collections.Counter(fake_guard_send.by_schema)
            body = guard_body(QUESTION, candidate=candidate)
            r, calls = sent(client, fake_guard_send, "/guard", body)
            got = r.json().get("input")
            seen = (
                f"{toggles_seen(client, fake_guard_send, before)}, verdict={used}(force {force}),"
                f" 후보 답변 {candidate is not None}, input={got}, 이번 요청 {calls}회"
            )
            assert calls == judges + jb, seen
            # 1차가 탈옥이면 자료 밖 확인은 부르지 않는다(탈옥 2차와 한 요청에서 같이 부르지 않음)
            assert fake_guard_send.by_schema["OomCheckOut"] == before["OomCheckOut"], seen
            assert_guard_contract(r.json(), has_candidate=candidate is not None)
            assert got == expected_input(jb, used), seen
            if jb:
                assert fake_guard_send.by_schema["JbCheckOut"] - jb_before == 1, seen
                counts[(used, candidate is not None)] += calls
    if jb:  # 2차 판정이 apply · study 어느 쪽이어도 호출 수가 같다(study는 기본값 + 명시 두 번)
        assert counts[("apply", False)] == 2 and counts[("apply", True)] == 3, counts
        assert counts[("study", False)] == 4 and counts[("study", True)] == 6, counts


OOM_VERDICTS = ("not_asked", "stated", "not_stated")  # 가드 #215 OomCheckOut.verdict
OUT_OF_MATERIAL = {"decision": "allow", "category": "out_of_material"}


def test_normal_question_calls_the_oom_check_when_it_is_on(real_guard_client, fake_guard_send):
    """1차 입력 판정이 normal인 일반 질문. 자료 밖 확인이 켜져 있으면 요청마다 nano 1회
    (OomCheckOut)가 더해지고, verdict가 not_stated면 input이 allow · out_of_material, 그 밖에는
    allow · normal이다. 꺼져 있거나 토글이 없으면(main) 0회이고 input은 force와 상관없이 normal.
    #215 전의 main, #215 그대로(끔), #215에서 켠 상태 모두에서 통과한다."""
    client = real_guard_client
    oom = oom_check_on(client)
    for verdict in OOM_VERDICTS:
        force = {"category": "normal", "verdict": verdict}
        fake_guard_send.force = force
        before = collections.Counter(fake_guard_send.by_schema)
        r, calls = sent(client, fake_guard_send, "/guard", guard_body(QUESTION))
        got = r.json().get("input")
        seen = (
            f"{toggles_seen(client, fake_guard_send, before)}, force {force}, input={got},"
            f" 이번 요청 {calls}회"
        )
        assert fake_guard_send.by_schema["OomCheckOut"] - before["OomCheckOut"] == int(oom), seen
        assert calls == 1 + int(oom), seen
        assert_guard_contract(r.json(), has_candidate=False)
        want = OUT_OF_MATERIAL if oom and verdict == "not_stated" else ALLOWED_NORMAL
        assert got == want, seen
    fake_guard_send.force = {}  # 뒤 요청에 남기지 않는다(픽스처도 테스트마다 새로 만든다)


def test_tutor_first_judge_jailbreak_calls(real_guard_client, fake_guard_send):
    """`/tutor`에서 가드 classify가 탈옥을 낸 경로(튜터의 가드 두 단계를 켠 진짜 판정기 앱).

    2차가 꺼져 있으면(main) classify 1회(InputOut)로 끝난다. 켜져 있으면 2차(JbCheckOut)가 더해지고,
    study면 일반으로 바뀌어 judge_output(OutputOut)까지 3회 = `/tutor` 상한, apply면 2회다.
    튜터가 낸 action은 튜터 소유 규칙이라 보지 않는다.
    """
    client = real_guard_client
    guard = client.app.state.guard
    assert all(hasattr(guard, name) for name in TUTOR_GUARD_METHODS), TUTOR_GUARD_HINT
    jb = jb_check_on(client)
    cases = (("study", 3), ("apply", 2)) if jb else ((None, 1),)
    for verdict, expected in cases:
        force = dict(JAILBREAK) if verdict is None else {**JAILBREAK, "verdict": verdict}
        fake_guard_send.force = force
        before = collections.Counter(fake_guard_send.by_schema)
        _, calls = sent(client, fake_guard_send, "/tutor", user(QUESTION))
        delta = dict(collections.Counter(fake_guard_send.by_schema) - before)
        seen = f"jb_check={jb}, verdict={verdict}, 이번 요청 by_schema={delta} · {calls}회"
        assert calls == expected, seen + " " + TUTOR_GUARD_HINT


def test_tutor_hands_its_judges_to_the_real_guard(real_guard_client, fake_guard_send, monkeypatch):
    app = real_guard_client.app
    has = {name: hasattr(app.state.guard, name) for name in TUTOR_GUARD_METHODS}
    # 튜터는 가드의 classify · judge_output을 부른다. 지금 main의 Guard에는 둘이 없다(이슈 #119).
    # 가드가 올리면 이 테스트는 고치지 않아도 첫 분기로 넘어간다
    if all(has.values()):
        assert_tutor_uses_guard(real_guard_client, fake_guard_send, monkeypatch)
    elif any(has.values()):
        pytest.fail(f"이슈 #119: classify와 judge_output은 함께 올려야 한다({has})")
    else:  # 지금 main: 튜터가 두 단계를 건너뛰고 사유만 센다
        t, calls = sent(real_guard_client, fake_guard_send, "/tutor", user(QUESTION))
        assert_tutor_contract(t.json())
        assert t.json()["answer"].startswith(ANSWER_WORD)
        assert calls == 0
        assert dict(app.state.guard_skips) == {("classify", "missing"): 1, ("judge", "missing"): 1}


# 가드 메서드가 들어오기 전에도 위 첫 분기의 검사(`assert_tutor_uses_guard`)가 무엇을 잡는지 본다.
# 진짜 판정기 인스턴스에 임시 메서드를 단다(테스트가 끝나면 monkeypatch가 뗀다).


async def _classify_normal(messages):
    return "normal"


async def _classify_raises(messages):
    raise RuntimeError("가드 classify 실패 흉내")


async def _judge_grounded(messages, candidate_answer, category=None):
    return OutputVerdict(True, (), True)


async def _judge_not_a_verdict(messages, candidate_answer, category=None):
    return {"grounded": True, "unsupported": [], "safe": True}  # 판정 객체가 아니다


def _attach(monkeypatch, guard, classify, judge_output) -> None:
    monkeypatch.setattr(guard, "classify", classify, raising=False)
    monkeypatch.setattr(guard, "judge_output", judge_output, raising=False)


def test_tutor_guard_check_passes_for_a_guard_with_both_methods(
    real_guard_client, fake_guard_send, monkeypatch
):
    _attach(monkeypatch, real_guard_client.app.state.guard, _classify_normal, _judge_grounded)
    assert_tutor_uses_guard(real_guard_client, fake_guard_send, monkeypatch)


@pytest.mark.parametrize(
    ("classify", "judge_output"),
    [(_classify_normal, _judge_not_a_verdict), (_classify_raises, _judge_grounded)],
    ids=["judge_output_returns_no_verdict", "classify_raises"],
)
def test_tutor_guard_check_catches_a_broken_guard_method(
    real_guard_client, fake_guard_send, monkeypatch, classify, judge_output
):
    _attach(monkeypatch, real_guard_client.app.state.guard, classify, judge_output)
    with pytest.raises(AssertionError, match="interface.md"):
        assert_tutor_uses_guard(real_guard_client, fake_guard_send, monkeypatch)


def test_tutor_guard_check_catches_a_tutor_that_skips_judge_output(
    open_real_guard_app, fake_guard_send, monkeypatch, tmp_path
):
    # classify만 켜고 가드 검증은 끈 설정(toggles.verify: off): 튜터가 judge_output을 부르지 않는다
    path = write_tutor_config(tmp_path / "tutor.yaml", guard_classify="parallel", verify="off")
    monkeypatch.setenv("TUTOR_CONFIG", str(path))
    with open_real_guard_app() as client:
        toggles = client.app.state.tutor_cfg.toggles
        assert (toggles.guard_classify, toggles.verify) == ("parallel", "off")
        _attach(monkeypatch, client.app.state.guard, _classify_normal, _judge_grounded)
        with pytest.raises(AssertionError, match="interface.md"):
            assert_tutor_uses_guard(client, fake_guard_send, monkeypatch)


def test_shipped_tutor_config_calls_no_guard_stage(
    open_real_guard_app, fake_guard_send, monkeypatch
):
    # 배포 설정(11/14 끔): 가드에 classify · judge_output이 있어도 튜터는 부르지 않고 건너뜀으로도
    # 세지 않는다. 가드 nano도 0회다
    called: collections.Counter[str] = collections.Counter()

    async def classify(messages):
        called["classify"] += 1
        return "normal"

    async def judge_output(messages, candidate_answer, category=None):
        called["judge_output"] += 1
        return OutputVerdict(True, (), True)

    with open_real_guard_app() as client:
        app = client.app
        toggles = app.state.tutor_cfg.toggles
        assert (toggles.guard_classify, toggles.verify) == ("off", "off")
        _attach(monkeypatch, app.state.guard, classify, judge_output)
        t, calls = sent(client, fake_guard_send, "/tutor", user(QUESTION))
        assert_tutor_contract(t.json())
        assert t.json()["answer"].startswith(ANSWER_WORD)  # 주 호출 답이 그대로 나갔다
        assert called == {} and calls == 0
        assert dict(app.state.guard_skips) == {}


# 표식이 든 요청들: 후보 답변이 있는 /guard, /tutor
MARKED_SENDS = [
    ("/guard", guard_body(f"{MARKER} {QUESTION}", candidate=f"{MARKER} {CANDIDATE}")),
    ("/tutor", user(f"{MARKER} {QUESTION}")),
]
# 1차 입력 판정이 탈옥인 /guard(탈옥 2차 판정이 켜져 있으면 그 경로도 지난다)
MARKED_JAILBREAK = guard_body(f"{MARKER} {QUESTION}", candidate=f"{MARKER} {CANDIDATE}")


def test_request_content_is_not_kept_with_the_real_judge(
    real_guard_client, fake_guard_send, caplog, capfd
):
    client, app = real_guard_client, real_guard_client.app
    needles = (MARKER, GUARD_WORD + " ", ANSWER_WORD + " ")
    assert find_text(state_roots(app), needles) == []
    caches_before = cache_sizes()
    texts = []
    sends = [(path, body, {}) for path, body in MARKED_SENDS]
    sends.append(("/guard", MARKED_JAILBREAK, JAILBREAK))
    for path, body, force in sends:
        fake_guard_send.force = dict(force)
        r, _ = sent(client, fake_guard_send, path, body)
        texts.append(r.text)
        # 요청마다 본다(뒤 요청이 앞 요청의 흔적을 덮어쓴 경우를 놓치지 않게)
        assert find_text(state_roots(app), needles) == [], path
        assert cache_sizes() == caches_before, path
    assert GUARD_WORD not in "".join(texts)  # 판정 글은 응답에 나오지 않는다
    # 판정 글과 요청 문구는 로그(DEBUG까지)에 나오지 않는다
    logged = "\n".join(rec.getMessage() for rec in caplog.records) + caplog.text
    assert MARKER not in logged and GUARD_WORD not in logged
    # print나 전파되지 않는 로거의 자체 핸들러로 새는 경우: stdout · stderr에도 없다
    out, err = capfd.readouterr()
    for text in (out, err):
        assert MARKER not in text and GUARD_WORD not in text


def test_real_judge_calls_stay_within_the_budget(real_guard_client, fake_guard_send):
    client = real_guard_client
    for path, body in [("/guard", guard_body(QUESTION)), *MARKED_SENDS]:
        _, calls = sent(client, fake_guard_send, path, body)
        limit = guard_calls_per_request(path, real_guard=True)
        assert calls <= limit, (path, calls, limit, GUARD_BUDGET_HINT)
    assert fake_guard_send.count >= 3  # 진짜 판정기가 실제로 nano를 불렀다(/guard 1 + 2)
