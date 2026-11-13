"""가드 LLM(nano)의 가짜 전송과 호출 수 집계(가드 S2.8 대비).

지금 루트 앱의 가드는 상수 판정기라 nano를 부르지 않는다. 가드가 기본 판정기를 실제 판정기로
바꾸면(S2.8) 통합 테스트가 띄우는 앱이 기동 때 가드 LLM을 만든다. 그때 필요한 것을 미리 확인한다.

- 가드 LLM을 서버 기본 경로(`LLM(cfg.llm)` → `start()`)로 만들어도 실제 클라이언트 대신 가짜로 간다.
- 가짜는 가드 판정 스키마 셋을 두 출력 방식(json_schema · json_object) 모두에서 알아본다.
- 판정기가 nano를 부르는 앱(대역 `llm_guard`)이 뜨고, 두 경로가 규격대로 답하고, 호출 수가 세어지고,
  판정 글과 요청 내용이 응답 · 로그 · 앱 상태에 남지 않는다.
- 루트 앱의 가드 nano 호출 수는 `applib.MAX_GUARD_LLM_CALLS_PER_REQUEST` 안이다.
"""

import asyncio
import logging
import time

import pytest
from guardrail.config import load_config
from guardrail.judges.input_judge import judge_input
from guardrail.judges.schemas import InputOut, OutputBaselineOut, OutputOut
from guardrail.llm import LLM
from guardrail.schema import Message
from pydantic import BaseModel

from tests.integration.applib import (
    ANSWER_WORD,
    AUTH,
    CATEGORIES,
    GUARD_BUDGET_HINT,
    GUARD_WORD,
    MAX_LLM_CALLS_PER_REQUEST,
    FakeGuardSend,
    UnknownSchema,
    as_bytes,
    assert_guard_contract,
    assert_tutor_contract,
    cache_sizes,
    find_text,
    guard_call_budget,
    state_roots,
    user,
)

MARKER = "가드표식QX7"
QUESTION = f"{MARKER} 리스트에 값을 넣는 방법이 뭐야?"
CANDIDATE = f"{MARKER} append를 쓰면 돼요."
PROMPT = [
    {"role": "system", "content": "판정"},
    {"role": "user", "content": "리스트에 값을 넣는 방법이 뭐야?"},
]


def post(client, path: str, body: dict, headers=AUTH):
    return client.post(path, content=as_bytes(body), headers=headers, follow_redirects=False)


def guard_body(content: str, candidate: str | None = None) -> dict:
    body = user(content)
    if candidate is not None:
        body["candidate_answer"] = candidate
    return body


def complete_once(llm_factory, schema, *, structured: str | None = None):
    """LLM을 만들고 start() 뒤 한 번 부른다. (llm, 결과)를 돌려준다."""

    async def run():
        llm = llm_factory()
        await llm.start()
        if structured is not None:
            llm.structured = structured
        result = await llm.complete("input_judge", PROMPT, schema, deadline=time.monotonic() + 5)
        return llm, result

    return asyncio.run(run())


# ── 가짜 전송 ─────────────────────────────────────────────────────────────


def test_guard_llm_built_the_server_way_talks_to_the_fake(app_env, fake_guard_send):
    """send를 주지 않고 만든 가드 LLM(서버 기본 경로)이 실제 클라이언트 대신 가짜로 간다.

    가드가 클라이언트를 만드는 자리(`guardrail.llm`의 `openai_env` · `openai.AsyncOpenAI`)를 옮기면
    여기서 걸린다. 그때는 conftest의 `fake_guard_send`를 새 자리에 맞춘다.
    """
    llm, result = complete_once(lambda: LLM(load_config().llm), InputOut)
    assert fake_guard_send.clients == 1
    assert fake_guard_send.probes >= 1  # start()의 probe도 가짜가 받았다
    assert result.error is None and result.attempts == 1
    assert result.parsed.category == "normal" and GUARD_WORD in result.parsed.evidence
    assert fake_guard_send.count == 1  # probe는 판정 호출로 세지 않는다
    assert dict(llm.calls) == {"input_judge": 1}
    assert dict(llm.unexpected) == {}
    assert llm.model == "fake-model"


@pytest.mark.parametrize("structured", ["json_schema", "json_object"])
@pytest.mark.parametrize("schema", [InputOut, OutputOut, OutputBaselineOut])
def test_fake_answers_every_guard_schema_in_both_output_modes(app_env, schema, structured):
    fake = FakeGuardSend()
    llm, result = complete_once(
        lambda: LLM(load_config().llm, send=fake), schema, structured=structured
    )
    assert result.error is None and isinstance(result.parsed, schema)
    assert dict(fake.by_schema) == {schema.__name__: 1} and fake.unknown == []
    assert dict(llm.unexpected) == {}


@pytest.mark.parametrize("structured", ["json_schema", "json_object"])
def test_fake_answers_the_guards_real_input_judge(app_env, structured):
    """대역이 아니라 가드의 실제 입력 판정 함수(`judge_input`)가 만든 요청을 가짜가 알아본다.

    json_object 방식에서는 스키마가 프롬프트 끝에 붙는다. 실제 프롬프트 본문에 스키마 필드 이름이
    나와도 가짜가 다른 스키마로 착각하지 않는지 여기서 본다.
    """
    fake = FakeGuardSend()
    cfg = load_config()
    question = "리스트에 값을 넣는 방법이 뭐야?"

    async def run():
        llm = LLM(cfg.llm, send=fake)
        await llm.start()
        llm.structured = structured
        window = [Message("user", question)]
        category = await judge_input(window, question, [], llm, cfg, deadline=time.monotonic() + 5)
        return llm, category

    llm, category = asyncio.run(run())
    assert category == "normal"
    assert dict(fake.by_schema) == {"InputOut": 1} and fake.unknown == []
    assert dict(llm.unexpected) == {}


class NewJudgeOut(BaseModel):
    verdict: str


def test_unknown_guard_schema_is_recorded(app_env):
    """가드 LLM은 전송 예외를 오류 코드로 삼킨다. 가짜가 모르는 스키마는 `unknown`에 남는다
    (`fake_guard_send` 픽스처가 끝날 때 그것을 보고 테스트를 실패시킨다)."""
    fake = FakeGuardSend()
    llm, result = complete_once(lambda: LLM(load_config().llm, send=fake), NewJudgeOut)
    assert result.error == "server" and result.parsed is None
    assert fake.unknown == ["NewJudgeOut"]
    assert dict(llm.unexpected) == {UnknownSchema.__name__: 1}


def test_fake_guard_verdicts_vary_with_the_prompt_and_repeat_for_the_same_prompt():
    fake = FakeGuardSend(vary=True)
    spec = {"type": "json_schema", "json_schema": {"name": "InputOut"}}

    async def ask(text: str) -> str:
        messages = [{"role": "system", "content": "판정"}, {"role": "user", "content": text}]
        reply = await fake({"messages": messages, "response_format": spec})
        return InputOut.model_validate_json(reply.choices[0].message.content).category

    async def run():
        first = [await ask(f"질문 {i}") for i in range(60)]
        again = [await ask(f"질문 {i}") for i in range(60)]
        return first, again

    first, again = asyncio.run(run())
    assert first == again  # 무작위가 아니다: 같은 프롬프트면 같은 판정
    assert set(first) <= set(CATEGORIES) and len(set(first)) >= 6
    assert fake.count == 120 and fake.probes == 0


# ── 판정기가 nano를 부르는 앱(대역) ───────────────────────────────────────


def test_app_whose_guard_calls_nano_starts_and_serves_both_paths(
    open_llm_guard_app, fake_send, fake_guard_send, caplog
):
    caplog.set_level(logging.DEBUG)
    with open_llm_guard_app() as client:
        llm = client.app.state.guard.llm
        # 기동: 가드 LLM이 가짜 클라이언트로 probe까지 마쳤고, 판정 호출은 아직 없다
        assert fake_guard_send.clients == 1 and fake_guard_send.probes >= 1
        assert fake_guard_send.count == 0

        g = post(client, "/guard", guard_body(QUESTION))
        assert g.status_code == 200
        assert_guard_contract(g.json(), has_candidate=False)
        assert fake_guard_send.count == 1  # 입력 판정

        g2 = post(client, "/guard", guard_body(QUESTION, candidate=CANDIDATE))
        assert g2.status_code == 200
        assert_guard_contract(g2.json(), has_candidate=True)
        assert fake_guard_send.count == 3  # 입력 판정 + 출력 판정
        assert fake_send.count == 0  # 가드는 튜터 전송을 쓰지 않는다

        before = fake_guard_send.count
        t = post(client, "/tutor", user(QUESTION))
        assert t.status_code == 200
        assert_tutor_contract(t.json())
        assert t.json()["answer"].startswith(ANSWER_WORD)  # 튜터가 가짜 LLM의 답을 실제로 냈다
        assert fake_send.count == MAX_LLM_CALLS_PER_REQUEST["v2"]
        # 튜터가 가드에 맡기는 판정은 classify · judge_output 둘뿐이다(붙기 전에는 0)
        assert 0 <= fake_guard_send.count - before <= 2

        # 가드 LLM의 카운터와 가짜 전송이 센 수가 같다(재시도 없음, probe 제외)
        assert sum(llm.calls.values()) == fake_guard_send.count
        assert dict(llm.unexpected) == {}

    # 판정 글(evidence · reason)과 요청 문구는 응답 · 로그에 나오지 않는다
    logged = "\n".join(rec.getMessage() for rec in caplog.records) + caplog.text
    assert GUARD_WORD not in g.text + g2.text + t.text
    assert GUARD_WORD not in logged and MARKER not in logged


def test_requests_leave_nothing_behind_when_the_guard_calls_nano(
    open_llm_guard_app, fake_send, fake_guard_send
):
    with open_llm_guard_app() as client:
        app = client.app
        needles = (MARKER, GUARD_WORD + " ", ANSWER_WORD + " ")
        assert find_text(state_roots(app), needles) == []
        caches_before = cache_sizes()
        sends = [
            lambda: post(client, "/guard", guard_body(QUESTION)),
            lambda: post(client, "/guard", guard_body(QUESTION, candidate=CANDIDATE)),
            lambda: post(client, "/tutor", user(QUESTION)),
        ]
        for send in sends:
            assert send().status_code == 200
            assert find_text(state_roots(app), needles) == []
            assert cache_sizes() == caches_before
        assert fake_guard_send.count >= 3 and fake_send.count == 1


def test_same_guard_request_twice_calls_nano_twice(open_llm_guard_app, fake_guard_send):
    """판정 결과를 요청 사이에 저장 · 재사용하지 않는다: 같은 요청 두 번이면 nano도 두 번이다."""
    with open_llm_guard_app() as client:
        first = post(client, "/guard", guard_body(QUESTION))
        second = post(client, "/guard", guard_body(QUESTION))
    assert fake_guard_send.count == 2
    assert fake_guard_send.digests[0] == fake_guard_send.digests[1]
    assert first.json() == second.json()


# ── 루트 앱 ───────────────────────────────────────────────────────────────


def test_root_app_guard_nano_calls_stay_within_the_budget(client, fake_send, fake_guard_send):
    bodies = [
        guard_body(QUESTION),
        guard_body(QUESTION, candidate=CANDIDATE),
        guard_body("ㅋㅋㅋ"),
        guard_body(QUESTION, candidate=""),
    ]
    for sent, body in enumerate(bodies, start=1):
        assert post(client, "/guard", body).status_code == 200
        assert fake_guard_send.count <= guard_call_budget(guard=sent), GUARD_BUDGET_HINT
    assert fake_send.count == 0  # 가드는 튜터 전송을 쓰지 않는다
