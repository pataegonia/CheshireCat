"""주 호출 초안 하나를 medium + 스트리밍으로(실험, exp/tutor-medium-stream). LLM은 가짜다(API 0회).

- `Draft.stream`·`Draft.max_completion_tokens`가 없으면 지금과 같은 요청이 나간다.
- 스트림 초안은 조각을 이어 붙여 같은 파싱(`TutorLLM._parse`)을 지난다. 빈 스트림·잘림·중간 시간
  초과·중간 끊김은 그 초안만 무효(오류 코드)이고 예외가 밖으로 새지 않는다.
- 실험 설정 `config/tutor.medium-stream.yaml`은 `config/tutor.yaml`과 drafts만 다르다.
"""

import asyncio
import contextlib
import copy
import types

import httpx
import openai
import pytest
import yaml

from tests.tutor import test_multi_draft as tmd
from tests.tutor import tutor_testlib
from tests.tutor.test_flow import QUESTION, state_with
from tests.tutor.test_tutor_llm import Out, base, make, ok
from tutor.judge import flow
from tutor.request import parse_request
from tutor.runtime import config as tutor_config
from tutor.runtime import llm as tutor_llm
from tutor.runtime.config import load_config

REPO_CFG = tutor_config.ROOT / "config" / "tutor.yaml"
STREAM_CFG = tutor_config.ROOT / "config" / "tutor.medium-stream.yaml"
STREAM_DRAFTS = [
    {"effort": "low"},
    {"effort": "low"},
    {"effort": "medium", "stream": True, "max_completion_tokens": 6000},
]
PARAMS = {"reasoning_effort": "low", "verbosity": "low", "max_completion_tokens": 2000}


# --- 가짜 스트림 ---


def chunk(text=None, finish=None, tokens=None):
    choices = []
    if text is not None or finish is not None:
        delta = types.SimpleNamespace(content=text)
        choices = [types.SimpleNamespace(delta=delta, finish_reason=finish)]
    usage = types.SimpleNamespace(completion_tokens=tokens) if tokens is not None else None
    return types.SimpleNamespace(choices=choices, usage=usage)


class FakeStream:
    """조각을 차례로 내는 비동기 스트림. hang이면 마지막 조각 뒤 그만큼 멈추고, fail이면 그 예외를
    던진다. 닫혔는지 기록한다."""

    def __init__(self, *chunks, hang=None, fail=None):
        self.chunks = list(chunks)
        self.hang, self.fail = hang, fail
        self.closed = False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self.chunks:
            yield c
        if self.hang is not None:
            await asyncio.sleep(self.hang)
        if self.fail is not None:
            raise self.fail

    async def close(self):
        self.closed = True


class StreamSend:
    """stream=True 요청에는 stream을, 아니면 비스트리밍 응답을 준다. 보낸 요청을 기록한다."""

    def __init__(self, stream=None, reply=None):
        self.stream = stream
        self.reply = reply or ok()
        self.sent: list[dict] = []

    async def __call__(self, kwargs):
        self.sent.append(kwargs)
        return self.stream if kwargs.get("stream") else self.reply


def call(llm, clock, *, timeout_s=8.0, retries=0, **kw):
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    return llm.complete(
        "main", msgs, Out, deadline=clock() + 20, timeout_s=timeout_s, retries=retries, **kw
    )


def stream_llm(stream, params=PARAMS):
    send = StreamSend(stream)
    llm, clock = make(send, src=base(params))
    return llm, clock, send


# --- TutorLLM 스트림 ---


def test_stream_joins_chunks_and_goes_through_the_same_parse():
    stream = FakeStream(
        chunk('{"answ'), chunk('er": '), chunk('"a"}'), chunk(finish="stop"), chunk(tokens=3200)
    )
    llm, clock, send = stream_llm(stream)
    result = asyncio.run(call(llm, clock, effort="medium", stream=True, max_completion_tokens=6000))
    assert result.error is None and result.parsed == Out(answer="a")
    assert (result.finish_reason, result.completion_tokens) == ("stop", 3200)
    kwargs = send.sent[0]
    assert kwargs["stream"] is True and kwargs["max_completion_tokens"] == 6000
    assert kwargs["reasoning_effort"] == "medium"
    assert stream.closed  # 끝까지 읽은 스트림도 닫는다


@pytest.mark.parametrize("finish, cause", [("length", "length"), ("stop", "no_content")])
def test_empty_stream_is_an_invalid_draft(finish, cause):
    stream = FakeStream(chunk(finish=finish), chunk(tokens=6000))
    llm, clock, _send = stream_llm(stream)
    result = asyncio.run(call(llm, clock, stream=True))
    assert result.error == "bad_json" and result.parsed is None and result.attempts == 1
    assert llm.attempt_errors == {("main", cause): 1} and stream.closed


def test_stream_ending_with_length_is_an_invalid_draft_with_the_length_code():
    stream = FakeStream(chunk('{"answer": "잘린'), chunk(finish="length"), chunk(tokens=6000))
    llm, clock, _send = stream_llm(stream)
    result = asyncio.run(call(llm, clock, stream=True, max_completion_tokens=6000))
    assert result.error == "bad_json" and result.finish_reason == "length"
    assert flow._draft_code(result) == "length"  # /tutor 로그 초안 칸의 코드


def test_stream_stalling_past_the_draft_limit_is_a_timeout_and_is_closed():
    stream = FakeStream(chunk('{"answer"'), hang=30.0)
    llm, clock, _send = stream_llm(stream)

    async def go():
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        result = await call(llm, clock, timeout_s=0.2, stream=True)
        return result, loop.time() - t0

    result, took = asyncio.run(go())
    assert result.error == "timeout" and result.parsed is None
    assert took < 2.0  # 보내기 + 읽기 전체가 호출 제한(0.2초) 안에서 끊긴다
    assert stream.closed and flow._draft_code(result) == "timeout"


def test_stream_broken_mid_way_is_a_code_not_an_exception():
    req = httpx.Request("POST", "http://fake.invalid/v1/chat/completions")
    stream = FakeStream(chunk('{"answer"'), fail=openai.APIConnectionError(request=req))
    llm, clock, _send = stream_llm(stream)
    result = asyncio.run(call(llm, clock, stream=True))
    assert result.error == "server" and result.parsed is None and stream.closed


def test_cancelling_a_stream_draft_closes_the_stream():
    stream = FakeStream(chunk('{"answer"'), hang=30.0)
    llm, clock, _send = stream_llm(stream)

    async def go():
        task = asyncio.ensure_future(call(llm, clock, timeout_s=10.0, stream=True))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):  # 취소는 삼키지 않는다
            await task

    asyncio.run(go())
    assert stream.closed  # 기다리기에서 취소된 초안도 남는 연결이 없다(TU-18)


def test_non_stream_request_is_unchanged():
    plain, explicit = StreamSend(), StreamSend()
    for send, kw in ((plain, {}), (explicit, {"stream": False, "max_completion_tokens": None})):
        llm, clock = make(send, src=base(PARAMS))
        assert asyncio.run(call(llm, clock, **kw)).error is None
    assert plain.sent == explicit.sent
    assert "stream" not in plain.sent[0] and plain.sent[0]["max_completion_tokens"] == 2000


def test_probe_disabled_max_tokens_stays_off_for_a_stream_draft():
    stream = FakeStream(chunk('{"answer": "a"}', finish="stop"))
    off = {**PARAMS, "max_completion_tokens": None}
    llm, clock, send = stream_llm(stream, params=off)
    asyncio.run(call(llm, clock, stream=True, max_completion_tokens=6000))
    assert "max_completion_tokens" not in send.sent[0] and send.sent[0]["stream"] is True


# --- flow: 초안별로 넘긴다 ---


MD_STREAM = {**tmd.MD, "llm.multi_draft.drafts": STREAM_DRAFTS}


def test_only_the_stream_draft_gets_stream_and_its_token_cap():
    llm = tmd.ScriptLLM(*[(0.0, tmd.ok(tmd.GOOD_OUT))] * 3)
    tmd.run_flow(llm, **MD_STREAM)
    a, b, c = llm.calls
    assert c["stream"] is True and c["max_completion_tokens"] == 6000 and c["effort"] == "medium"
    for low in (a, b):  # 지금의 초안 호출과 같은 인자만 간다
        assert set(low) == {"role", "schema", "deadline", "timeout_s", "retries", "effort"}


def _sent_by_tutor_llm(drafts):
    """진짜 TutorLLM(가짜 전송)으로 초안을 보내고 전송 요청을 돌려준다."""
    sent: list[dict] = []

    async def send(kwargs):
        sent.append(kwargs)
        reply = tutor_testlib._ok_reply(kwargs)
        if not kwargs.get("stream"):
            return reply
        content = reply.choices[0].message.content
        return FakeStream(chunk(content), chunk(finish="stop"), chunk(tokens=5))

    llm = tutor_llm.TutorLLM(send, contextlib.nullcontext, model="m", base=base(PARAMS))
    state = state_with(llm, **{**tmd.MD, "llm.multi_draft.drafts": drafts})
    req = parse_request({"messages": [{"role": "user", "content": QUESTION}]})
    body = asyncio.run(flow.answer_v2(req, state, deadline=1e12))
    return body, sent


def test_through_tutor_llm_only_the_stream_draft_request_differs():
    before = [{"effort": "low"}, {"effort": "low"}, {"effort": "medium"}]
    body_old, old = _sent_by_tutor_llm(before)
    body_new, new = _sent_by_tutor_llm(STREAM_DRAFTS)
    assert len(old) == len(new) == 3 and body_old == body_new
    assert [k.get("stream") for k in new] == [None, None, True]
    assert new[:2] == old[:2]  # low 초안 요청은 한 글자도 같다
    changed = {k for k in new[2].keys() | old[2].keys() if new[2].get(k) != old[2].get(k)}
    assert changed == {"stream", "max_completion_tokens"}


LOW_CASES = [
    ((0.0, tmd.ok(tmd.GOOD_OUT)), (0.0, tmd.ok(tmd.ABSTAIN_OUT))),  # 갈림 → A
    ((0.0, tmd.ok(tmd.ABSTAIN_OUT)), (0.0, tmd.ok(tmd.GOOD_OUT))),  # 갈림 → A
    ((0.0, tmd.ok(tmd.GOOD_OUT)), (0.0, tmd.ok(tmd.GOOD_OUT))),
    ((0.0, tmd.result(error="timeout")), (0.0, tmd.ok(tmd.GOOD_OUT))),
]
BAD_MEDIUM = [
    ("length", tmd.result(error="bad_json", finish="length")),
    ("timeout", tmd.result(error="timeout")),
    ("bad_json", tmd.result(error="bad_json", finish="stop")),
]


@pytest.mark.parametrize("lows", LOW_CASES)
@pytest.mark.parametrize("code, medium", BAD_MEDIUM)
def test_invalid_medium_draft_picks_like_the_two_low_drafts_alone(lows, code, medium):
    two = {**tmd.MD, "llm.multi_draft.drafts": STREAM_DRAFTS[:2]}
    body2, _t, sh2 = tmd.run_flow(tmd.ScriptLLM(*lows), **two)
    body3, _t, sh3 = tmd.run_flow(tmd.ScriptLLM(*lows, (0.0, medium)), **MD_STREAM)
    assert body3 == body2 and sh3.chosen == sh2.chosen and sh3.actions[:2] == sh2.actions
    assert sh3.drafts[2].code == code  # 로그 칸에서 잘림·시간 초과가 갈려 보인다
    assert ";med_c=" in sh3.log_field() and f"/{code}/" in sh3.log_field()


# --- 설정 ---


def test_default_config_never_sends_a_stream_call():
    cfg = load_config(REPO_CFG)
    assert all(not d.stream and d.max_completion_tokens is None for d in cfg.llm.multi_draft.drafts)
    sent: list[dict] = []

    async def send(kwargs):
        sent.append(kwargs)
        return tutor_testlib._ok_reply(kwargs)

    llm = tutor_llm.TutorLLM(send, contextlib.nullcontext, model="m", base=base(PARAMS))
    state = types.SimpleNamespace(tutor_cfg=cfg, tutor_kb=state_with(llm).tutor_kb, tutor_llm=llm)
    flow.prepare_judge(state)
    req = parse_request({"messages": [{"role": "user", "content": QUESTION}]})
    asyncio.run(flow.answer_v2(req, state, deadline=1e12))
    assert len(sent) >= 3  # 초안 셋 이상이 실제로 나갔다
    assert not any("stream" in k for k in sent)
    assert {k["max_completion_tokens"] for k in sent} == {2000}


def _drafts(**over):
    return load_config(overrides={**tmd.MD, "llm.multi_draft.drafts": [*STREAM_DRAFTS[:2], over]})


def test_draft_token_cap_depends_on_stream():
    assert _drafts(effort="medium", stream=True, max_completion_tokens=6000)
    assert _drafts(effort="medium", stream=True)  # 상한 없으면 llm.max_completion_tokens
    assert _drafts(effort="low", max_completion_tokens=2000)
    with pytest.raises(ValueError, match="비스트리밍"):
        _drafts(effort="medium", max_completion_tokens=2001)  # 프록시가 400을 낸다
    with pytest.raises(ValueError, match="스트리밍"):
        _drafts(effort="medium", stream=True, max_completion_tokens=6001)
    with pytest.raises(ValueError):
        _drafts(effort="medium", stream="yes")  # strict


def test_medium_stream_config_differs_from_the_repo_config_only_in_drafts():
    repo = yaml.safe_load(REPO_CFG.read_text(encoding="utf-8"))
    exp = yaml.safe_load(STREAM_CFG.read_text(encoding="utf-8"))
    assert exp["llm"]["multi_draft"]["drafts"] == STREAM_DRAFTS
    strip = copy.deepcopy
    repo_rest, exp_rest = strip(repo), strip(exp)
    del repo_rest["llm"]["multi_draft"]["drafts"], exp_rest["llm"]["multi_draft"]["drafts"]
    assert exp_rest == repo_rest


def test_medium_stream_config_loads_with_the_same_nano_call_count():
    repo, exp = load_config(REPO_CFG), load_config(STREAM_CFG)
    assert exp.max_nano_calls() == repo.max_nano_calls() == 7
    assert exp.max_nano_calls() <= tutor_config.MULTI_DRAFT_MAX_CALLS
    medium = exp.llm.multi_draft.drafts[2]
    assert (medium.effort, medium.stream, medium.max_completion_tokens) == ("medium", True, 6000)
