"""진짜 판정기를 켠 루트 앱(I1.10).

가드 기본 판정기(`guardrail.server.default_factory`)는 OPENAI_API_KEY가 있으면 진짜 판정기
(`Guard.build`), 없으면 상수 판정기다. 다른 통합 테스트는 `app_env`가 키를 지워 상수 판정기로 돈다.
여기서는 `real_guard_client`(가짜 키 · 닫힌 주소 + 가드 · 튜터 전송 가짜)로 진짜 판정기 + 튜터를
합친 앱을 띄운다.

- 기동: 진짜 판정기로 뜨고 probe를 마쳤고, 판정 호출은 아직 없다.
- `/guard`: 후보 답변이 없으면 nano 1회(입력 판정), 있으면 2회(+ 출력 판정).
- `/tutor` → 가드: 가드에 classify · judge_output이 있으면 튜터가 각각 정확히 1회 부르고 건너뛴
  단계가 없다(`assert_tutor_uses_guard`). 없으면 건너뛴 수만 센다.
- 요청 내용 · 판정 글이 앱 상태 · 모듈 · 로그 · stdout · stderr에 남지 않는다.
- 요청마다 가드 nano 호출이 상한(`applib.guard_call_budget(real_guard=True)`) 안이다.
"""

import collections
import re

import pytest
from guardrail.schema import OutputVerdict

from tests.integration.applib import (
    ANSWER_WORD,
    AUTH,
    GUARD_BUDGET_HINT,
    GUARD_WORD,
    ROOT,
    as_bytes,
    assert_guard_contract,
    assert_tutor_contract,
    cache_sizes,
    find_text,
    guard_calls_per_request,
    state_roots,
    user,
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
    판정해 nano를 부르지 않을 수 있어 nano 호출의 하한은 두지 않는다.
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
    seen = f"감싸개 횟수 {dict(called)}, guard_skips {skipped}, 가드 nano {calls}회. "
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


_VERIFY_LINE = re.compile(r"^(?P<head>[ \t]+verify:[ \t]*)guard\b", re.M)


def test_tutor_guard_check_catches_a_tutor_that_skips_judge_output(
    open_real_guard_app, fake_guard_send, monkeypatch, tmp_path
):
    # 튜터 설정에서 가드 검증을 끈 사본(toggles.verify: off): 튜터가 judge_output을 부르지 않는다
    text = (ROOT / "config" / "tutor.yaml").read_text(encoding="utf-8")
    changed, count = _VERIFY_LINE.subn(r'\g<head>"off"', text)  # 따옴표 없으면 YAML bool
    assert count == 1
    path = tmp_path / "tutor.yaml"
    path.write_text(changed, encoding="utf-8")
    monkeypatch.setenv("TUTOR_CONFIG", str(path))
    with open_real_guard_app() as client:
        assert client.app.state.tutor_cfg.toggles.verify == "off"
        _attach(monkeypatch, client.app.state.guard, _classify_normal, _judge_grounded)
        with pytest.raises(AssertionError, match="interface.md"):
            assert_tutor_uses_guard(client, fake_guard_send, monkeypatch)


# 표식이 든 요청들: 후보 답변이 있는 /guard, /tutor
MARKED_SENDS = [
    ("/guard", guard_body(f"{MARKER} {QUESTION}", candidate=f"{MARKER} {CANDIDATE}")),
    ("/tutor", user(f"{MARKER} {QUESTION}")),
]


def test_request_content_is_not_kept_with_the_real_judge(
    real_guard_client, fake_guard_send, caplog, capfd
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
