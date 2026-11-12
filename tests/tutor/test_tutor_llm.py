"""튜터 LLM 호출(TU-13): 가드 게이트 공유, 호출당 시간 제한, 재시도 1회(예산 안), 오류는 코드로."""

import asyncio
import contextlib
import json
import logging
import os
import types

import httpx
import openai
import pytest
from pydantic import BaseModel

from guardrail import llm as guard_llm_mod
from guardrail.config import load_config as load_guard_config
from guardrail.guard import ConstantGuard
from tutor.runtime import llm as tutor_llm
from tutor.runtime.config import load_config

REQ = httpx.Request("POST", "http://fake.invalid/v1/chat/completions")
GUARD_PARAMS = {"reasoning_effort": "low", "verbosity": "low", "max_completion_tokens": None}


class Out(BaseModel):
    answer: str


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def ok(content='{"answer": "a"}', finish_reason="stop", tokens=12):
    choice = types.SimpleNamespace(
        message=types.SimpleNamespace(content=content), finish_reason=finish_reason
    )
    usage = types.SimpleNamespace(completion_tokens=tokens)
    return types.SimpleNamespace(choices=[choice], usage=usage)


def status(code, headers=None):
    resp = httpx.Response(code, headers=headers or {}, request=REQ)
    return openai.APIStatusError("x", response=resp, body=None)


class Send:
    """단계마다 응답을 내거나 예외를 던지는 가짜 chat.completions.create."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.sent: list[dict] = []

    async def __call__(self, kwargs):
        self.sent.append(kwargs)
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def base(params=None, structured="json_schema"):
    return types.SimpleNamespace(params=dict(params or GUARD_PARAMS), structured=structured)


def make(send, *, src=None, overrides=None, gate=contextlib.nullcontext, clock=None):
    clock = clock or Clock()
    llm = tutor_llm.TutorLLM(
        send,
        gate,
        model="m",
        base=src or base(),
        overrides=overrides,
        clock=clock,
        sleep=clock.sleep,
        rand=lambda: 0.0,
    )
    return llm, clock


def call(llm, clock, *, budget=20.0, timeout_s=8.0, retries=1, messages=None):
    messages = messages or [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    return llm.complete(
        "main", messages, Out, deadline=clock() + budget, timeout_s=timeout_s, retries=retries
    )


def run(llm, clock, **kw):
    return asyncio.run(call(llm, clock, **kw))


def test_ok_sends_fixed_params_and_a_strict_schema():
    send = Send(ok())
    llm, clock = make(send)
    result = run(llm, clock)
    assert result.parsed == Out(answer="a") and result.error is None and result.attempts == 1
    assert (result.finish_reason, result.completion_tokens) == ("stop", 12)
    kwargs = send.sent[0]
    assert kwargs["model"] == "m" and kwargs["timeout"] == 8.0
    assert kwargs["reasoning_effort"] == "low" and kwargs["verbosity"] == "low"
    assert "max_completion_tokens" not in kwargs  # None은 보내지 않는다(probe가 끈 것)
    assert "temperature" not in kwargs  # TU-13: 보내지 않는다
    fmt = kwargs["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"]["additionalProperties"] is False
    assert llm.calls == {"main": 1}


def test_failures_are_counted_by_role_and_code_but_not_budget_stops():
    """오류로 끝난 호출을 역할·코드별로 센다(측정이 기본값 섞인 것을 알게). 예산 막힘은 blocked에."""
    llm, clock = make(Send(status(400), ok()))
    assert run(llm, clock).error == "bad_request"
    assert run(llm, clock).error is None
    llm.max_calls = 2
    assert run(llm, clock).error == "budget"
    assert llm.failures == {("main", "bad_request"): 1} and llm.blocked == {"main": 1}


def test_a_call_recovered_by_retry_is_not_a_failure():
    llm, clock = make(Send(status(503), ok()))
    assert run(llm, clock).error is None
    assert llm.failures == {} and llm.calls == {"main": 2}  # 시도 2회, 실패 0


@pytest.mark.parametrize(
    "steps, code",
    [
        ((status(503), status(503)), "server"),  # 재시도 뒤 최종 실패도 호출당 1회
        ((ok("not json"), ok("not json")), "bad_json"),
    ],
)
def test_a_failed_call_counts_once_with_its_final_code(steps, code):
    llm, clock = make(Send(*steps))
    assert run(llm, clock).error == code
    assert llm.failures == {("main", code): 1} and llm.calls == {"main": 2}


def test_a_send_timeout_counts_as_a_failure():
    async def slow(_kwargs):
        await asyncio.sleep(5)

    llm, clock = make(slow)
    result = asyncio.run(call(llm, clock, budget=0.2, timeout_s=0.05, retries=0))
    assert result.error == "timeout" and llm.failures == {("main", "timeout"): 1}


def test_a_gate_timeout_counts_as_a_failure_without_a_call():
    """게이트를 못 얻어 보내지 못해도 그 호출의 답은 기본값이다(실패 1, 시도 0)."""

    @contextlib.asynccontextmanager
    async def stuck():
        await asyncio.sleep(5)
        yield

    llm, clock = make(Send(ok()), gate=stuck)
    result = asyncio.run(call(llm, clock, budget=0.05, timeout_s=0.05, retries=1))
    assert result.error == "timeout" and llm.failures == {("main", "timeout"): 1}
    assert not llm.calls


def test_tutor_settings_override_only_params_the_probe_kept():
    send = Send(ok())
    src = base({"reasoning_effort": "low", "verbosity": "low", "max_completion_tokens": None})
    overrides = {"reasoning_effort": "medium", "max_completion_tokens": 1600}
    llm, clock = make(send, src=src, overrides=overrides)
    run(llm, clock)
    kwargs = send.sent[0]
    assert kwargs["reasoning_effort"] == "medium"  # 튜터 설정이 덮는다
    assert "max_completion_tokens" not in kwargs  # probe가 끈 것은 끈 채로


def test_params_and_format_follow_the_guard_probe_after_build():
    send = Send(ok(), ok())
    src = base()
    llm, clock = make(send, src=src)
    run(llm, clock)
    src.params["verbosity"] = None  # 가드 probe가 튜터를 만든 뒤에 돌아도 따른다
    src.structured = "json_object"
    run(llm, clock)
    later = send.sent[1]
    assert "verbosity" not in later and later["response_format"] == {"type": "json_object"}


def test_json_object_mode_puts_the_schema_in_the_system_message():
    send = Send(ok())
    llm, clock = make(send, src=base(structured="json_object"))
    assert run(llm, clock).error is None
    kwargs = send.sent[0]
    assert kwargs["response_format"] == {"type": "json_object"}
    assert "answer" in kwargs["messages"][0]["content"]  # 스키마를 시스템 메시지에 붙인다


def test_json_object_mode_adds_a_system_message_when_there_is_none():
    send = Send(ok())
    llm, clock = make(send, src=base(structured="json_object"))
    run(llm, clock, messages=[{"role": "user", "content": "q"}])
    first, second = send.sent[0]["messages"]
    assert first["role"] == "system" and "answer" in first["content"]
    assert second == {"role": "user", "content": "q"}


def test_per_call_limit_never_exceeds_the_remaining_budget():
    send = Send(ok())
    llm, clock = make(send)
    run(llm, clock, budget=3.0, timeout_s=8.0)
    assert send.sent[0]["timeout"] == 3.0


def test_per_call_limit_caps_the_whole_send():
    async def slow(_kwargs):
        await asyncio.sleep(5)

    llm = tutor_llm.TutorLLM(slow, contextlib.nullcontext, model="m", base=base())

    async def go():
        loop = asyncio.get_running_loop()
        return await llm.complete(
            "main", [], Out, deadline=loop.time() + 20, timeout_s=0.05, retries=0
        )

    result = asyncio.run(go())
    assert result.error == "timeout" and result.attempts == 1  # 8초 같은 호출 상한이 전체 시간


@pytest.mark.parametrize(
    "first, code",
    [
        (ok("not json"), "bad_json"),
        (ok(None), "bad_json"),
        (types.SimpleNamespace(choices=[]), "bad_json"),
        (openai.APITimeoutError(request=REQ), "timeout"),
        (openai.APIConnectionError(request=REQ), "server"),
        (status(500), "server"),
        (status(429), "rate_limit"),
        (status(408), "timeout"),
    ],
)
def test_retryable_failures_retry_once(first, code):
    send = Send(first, ok())
    llm, clock = make(send)
    result = run(llm, clock)
    assert result.error is None and result.attempts == 2

    send = Send(first, first)
    llm, clock = make(send)
    result = run(llm, clock)
    assert result.error == code and result.attempts == 2 and result.parsed is None


def test_truncated_answer_shows_as_length():
    llm, clock = make(Send(ok('{"answer": "a', finish_reason="length", tokens=1600)))
    result = run(llm, clock, retries=0)
    assert result.error == "bad_json" and result.finish_reason == "length"
    assert result.completion_tokens == 1600  # 상한(max_completion_tokens)을 정할 때 본다


@pytest.mark.parametrize(
    "failure, code",
    [
        (status(401), "auth"),
        (status(400), "bad_request"),
        (openai.OpenAIError("설정 오류"), "auth"),
        (RuntimeError("x"), "server"),
    ],
)
def test_other_failures_do_not_retry(failure, code):
    send = Send(failure, ok())
    llm, clock = make(send)
    result = run(llm, clock)
    assert result.error == code and result.attempts == 1


def test_unclassified_errors_are_counted_by_class_name_only():
    llm, clock = make(Send(KeyError("비밀 본문")))
    run(llm, clock)
    assert llm.unexpected == {"KeyError": 1}


def test_server_can_forbid_or_allow_a_retry():
    llm, clock = make(Send(status(500, {"x-should-retry": "false"}), ok()))
    assert run(llm, clock).attempts == 1
    llm, clock = make(Send(status(400, {"x-should-retry": "true"}), ok()))
    assert run(llm, clock).error is None


def test_no_retry_when_retries_is_zero():
    llm, clock = make(Send(status(500), ok()))
    result = run(llm, clock, retries=0)
    assert result.error == "server" and result.attempts == 1


def test_retry_waits_for_retry_after_inside_the_budget():
    llm, clock = make(Send(status(429, {"retry-after": "2"}), ok()))
    start = clock()
    assert run(llm, clock).error is None
    assert clock() - start == 2.0


@pytest.mark.parametrize("value", ["nan", "inf", "-3", "Wed, 21 Oct 2015 07:28:00 GMT"])
def test_unusable_retry_after_falls_back_to_jitter(value):
    send = Send(status(429, {"retry-after": value}), ok())
    llm, clock = make(send)
    start = clock()
    result = run(llm, clock)
    # 유한한 숫자가 아니면 가드와 같이 지터(0.2초)로 기다린다. NaN으로 멈추지 않는다
    assert result.error is None and clock() - start == pytest.approx(0.2)


def test_retry_after_beyond_the_budget_stops():
    send = Send(status(429, {"retry-after-ms": "15000"}), ok())
    llm, clock = make(send)
    result = run(llm, clock, budget=20.0, timeout_s=8.0)  # 15초 + 8초 > 20초
    assert result.error == "rate_limit" and len(send.sent) == 1


def test_retry_is_skipped_when_the_gate_wait_used_the_time():
    clock = Clock()
    entries = []

    @contextlib.asynccontextmanager
    async def gate():
        entries.append(1)
        if len(entries) == 2:
            clock.now += 15  # 두 번째(재시도)는 게이트에서 오래 기다렸다
        yield

    send = Send(status(500), ok())
    llm, clock = make(send, gate=gate, clock=clock)
    result = run(llm, clock, budget=20.0, timeout_s=8.0)
    assert result.error == "server" and result.attempts == 1 and len(send.sent) == 1


def test_gate_timeout_on_a_retry_keeps_the_first_error():
    lock = asyncio.Lock()
    gates = iter([contextlib.nullcontext(), lock])
    send = Send(status(500))

    async def go():
        llm = tutor_llm.TutorLLM(send, lambda: next(gates), model="m", base=base(), rand=lambda: 0)
        await lock.acquire()  # 재시도 때 다른 요청이 게이트를 쥐고 있다
        loop = asyncio.get_running_loop()
        return await llm.complete(
            "main", [], Out, deadline=loop.time() + 0.5, timeout_s=0.1, retries=1
        )

    result = asyncio.run(go())
    assert result.error == "server" and result.attempts == 1  # timeout으로 덮지 않는다


def test_expired_deadline_sends_nothing():
    send = Send(ok())
    llm, clock = make(send)
    result = run(llm, clock, budget=0.0)
    assert result.error == "timeout" and result.attempts == 0 and send.sent == []


def test_gate_wait_counts_toward_the_deadline():
    lock = asyncio.Lock()
    send = Send(ok())

    async def blocked():
        await lock.acquire()  # 다른 요청이 게이트를 쥐고 있다
        llm = tutor_llm.TutorLLM(send, lambda: lock, model="m", base=base())
        loop = asyncio.get_running_loop()
        return await llm.complete(
            "main", [], Out, deadline=loop.time() + 0.05, timeout_s=8.0, retries=1
        )

    result = asyncio.run(blocked())
    assert result.error == "timeout" and result.attempts == 0 and send.sent == []


def test_call_budget_stops_before_sending():
    send = Send(ok(), ok())
    llm, clock = make(send)
    llm.max_calls = 1
    assert run(llm, clock).error is None
    result = run(llm, clock)
    assert result.error == "budget" and result.attempts == 0 and len(send.sent) == 1
    assert llm.blocked == {"main": 1}  # 막혀 보내지 못한 호출을 역할별로 센다(#108 리뷰)


def test_cancel_is_not_swallowed():
    started = asyncio.Event()

    async def forever(_kwargs):
        started.set()
        await asyncio.Event().wait()

    async def cancel_midway():
        llm, clock = make(forever)
        task = asyncio.create_task(call(llm, clock))
        await started.wait()
        task.cancel()
        await task

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancel_midway())


def test_result_repr_hides_the_answer():
    llm, clock = make(Send(ok(json.dumps({"answer": "비밀 답변"}))))
    assert "비밀 답변" not in repr(run(llm, clock))  # 로그에 답변이 새지 않게(TU-15)
    assert "비밀 답변" not in repr(tutor_llm.TutorLLM._parse(ok("{}"), Out))


@pytest.mark.parametrize("code", [400, 401, 403, 404, 408, 409, 422, 429, 500, 502, 503])
def test_status_table_matches_the_guard(code):
    assert tutor_llm._status_code(code) == guard_llm_mod._status_code(code)


def test_build_shares_the_guard_gate_and_reads_its_probe_result(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "openai/gpt-5-nano")
    params = {"reasoning_effort": "low", "verbosity": None, "max_completion_tokens": 800}
    guard = types.SimpleNamespace(
        slot=contextlib.nullcontext, params=params, structured="json_object"
    )
    built = tutor_llm.build_llm(types.SimpleNamespace(guard=types.SimpleNamespace(llm=guard)), CFG)
    assert built.gate is guard.slot and built.model == "openai/gpt-5-nano"
    assert built.structured == "json_object"
    assert built.params == {
        "reasoning_effort": "low",
        "verbosity": None,
        "max_completion_tokens": 1600,
    }


def test_build_without_the_guard_uses_the_guard_config_values():
    built = tutor_llm.build_llm(types.SimpleNamespace(), CFG)
    assert built.params == {
        "reasoning_effort": "low",
        "verbosity": "low",
        "max_completion_tokens": 1600,  # 튜터 주 호출 상한(설정)
    }
    assert built.structured == "json_schema"


@pytest.mark.parametrize(
    "state",
    [
        types.SimpleNamespace(),  # 가드 없음(프로세스 안 측정)
        types.SimpleNamespace(guard=ConstantGuard()),  # 루트 앱 기본 판정기: LLM이 없다
    ],
)
def test_build_without_a_guard_llm_still_caps_concurrent_calls(monkeypatch, state):
    """가드에 LLM이 없어도 튜터 호출은 가드 설정 llm.gate만큼만 동시에 나간다(TU-13, #126 리뷰)."""
    size = load_guard_config().llm.gate
    release = asyncio.Event()
    live = {"now": 0, "peak": 0}

    async def held(_kwargs):
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        try:
            await release.wait()
        finally:
            live["now"] -= 1
        return ok()

    monkeypatch.setattr(tutor_llm, "_openai_send", lambda: held)
    built = tutor_llm.build_llm(state, CFG)

    async def flood():
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        calls = [
            asyncio.create_task(
                built.complete("main", [], Out, deadline=deadline, timeout_s=5, retries=0)
            )
            for _ in range(size + 6)
        ]
        for _ in range(200):  # 게이트에 든 호출이 모두 보내질 때까지 루프를 돌린다
            if live["now"] >= size:
                break
            await asyncio.sleep(0)
        sent_at_once = live["now"]
        release.set()
        return sent_at_once, await asyncio.gather(*calls)

    sent_at_once, results = asyncio.run(flood())
    assert sent_at_once == size and live["peak"] == size  # 상한까지 함께 나가고 넘지 않는다
    assert all(r.error is None for r in results)  # 기다린 호출도 마감 안에 나간다


def test_startup_builds_the_llm_on_the_guard_gate():
    import tutor

    guard = types.SimpleNamespace(slot=contextlib.nullcontext, params={}, structured="json_schema")
    state = types.SimpleNamespace(guard=types.SimpleNamespace(llm=guard))
    asyncio.run(tutor.startup(state))
    assert isinstance(state.tutor_llm, tutor_llm.TutorLLM)
    assert state.tutor_llm.gate is guard.slot


def test_startup_names_the_missing_api_env_without_values(monkeypatch, caplog):
    """서버가 OPENAI_* 없이 뜨면 모든 답이 기본값이 된다. 기동 때 빈 이름만 오류로 남긴다."""
    import tutor

    monkeypatch.setenv("OPENAI_API_KEY", "secret-key-value")
    monkeypatch.setenv("OPENAI_MODEL", "  ")  # 공백뿐인 값도 없는 것으로 본다
    # OPENAI_BASE_URL은 conftest가 지운 상태다
    with caplog.at_level(logging.ERROR, logger="tutor.startup"):
        asyncio.run(tutor.startup(types.SimpleNamespace()))
    text = "\n".join(r.getMessage() for r in caplog.records if r.name == "tutor.startup")
    assert "OPENAI_MODEL" in text and "OPENAI_BASE_URL" in text
    assert "OPENAI_API_KEY" not in text and "secret-key-value" not in text  # 값은 남기지 않는다


def test_startup_is_quiet_when_the_api_env_is_set(monkeypatch, caplog):
    import tutor

    for name, value in (("OPENAI_API_KEY", "k"), ("OPENAI_MODEL", "m")):
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9")
    with caplog.at_level(logging.ERROR, logger="tutor.startup"):
        asyncio.run(tutor.startup(types.SimpleNamespace()))
    assert [r for r in caplog.records if r.name == "tutor.startup"] == []


def test_build_strips_the_model_name(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "  openai/gpt-5-nano\n")  # .env 줄 끝 공백
    assert tutor_llm.build_llm(types.SimpleNamespace(), CFG).model == "openai/gpt-5-nano"


def test_shell_api_keys_never_reach_tests():
    """런박스 셸에 진짜 키가 있어도 테스트는 보지 못한다(conftest가 지운다, 운영진 API를 부르지 않게)."""
    names = ("OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL")
    names += ("EMBEDDING_API_KEY", "EMBEDDING_MODEL", "EMBEDDING_BASE_URL")
    assert [name for name in names if name in os.environ] == []


class FakeClient:
    made: list[dict] = []

    def __init__(self, **kwargs):
        FakeClient.made.append(kwargs)

        async def create(**kwargs):
            return ok()

        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=create))


@pytest.fixture
def fake_openai(monkeypatch):
    FakeClient.made = []
    monkeypatch.setattr(tutor_llm.openai, "AsyncOpenAI", FakeClient)
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9")
    return FakeClient.made


def test_openai_client_is_made_per_event_loop_without_sdk_retries(fake_openai):
    send = tutor_llm._openai_send()
    assert fake_openai == []  # 기동 때는 만들지 않는다

    async def twice():
        return [await send({"model": "m"}), await send({"model": "m"})]

    asyncio.run(twice())
    assert fake_openai == [{"api_key": "k", "base_url": "http://127.0.0.1:9", "max_retries": 0}]
    asyncio.run(twice())  # 다른 이벤트 루프(다른 asyncio.run)에서는 새로 만든다
    assert len(fake_openai) == 2


def _complete_once(llm):
    async def go():
        loop = asyncio.get_running_loop()
        return await llm.complete("main", [], Out, deadline=loop.time() + 5, timeout_s=1, retries=1)

    return asyncio.run(go())


@pytest.mark.parametrize("value", [None, "", "  "])
def test_missing_api_key_is_an_auth_error(fake_openai, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("OPENAI_API_KEY")
    else:
        monkeypatch.setenv("OPENAI_API_KEY", value)
    llm = tutor_llm.TutorLLM(
        tutor_llm._openai_send(), contextlib.nullcontext, model="m", base=base()
    )
    result = _complete_once(llm)
    assert result.error == "auth" and result.attempts == 1 and fake_openai == []


@pytest.mark.parametrize("value", [None, "", "  "])
def test_missing_base_url_is_an_auth_error_and_sends_nothing(fake_openai, monkeypatch, value):
    """주소가 비면 SDK 기본 주소(공개 OpenAI)로 보내지 않는다. 운영진 API만 부른다(SPEC §7)."""
    if value is None:
        monkeypatch.delenv("OPENAI_BASE_URL")
    else:
        monkeypatch.setenv("OPENAI_BASE_URL", value)
    llm = tutor_llm.TutorLLM(
        tutor_llm._openai_send(), contextlib.nullcontext, model="m", base=base()
    )
    result = _complete_once(llm)
    # 클라이언트도 만들지 않는다
    assert result.error == "auth" and result.attempts == 1 and fake_openai == []
    assert llm.calls["main"] == 1  # 보내지 못한 시도도 호출 수에 센다(키가 없을 때와 같음)


CFG = load_config()
