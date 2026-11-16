"""가드 LLM(nano)의 가짜 전송, 호출 수 집계, 판정기 대역 앱.

루트 앱의 기본 판정기(가드 `default_factory`)는 키가 없으면 상수 판정기라, 키를 지우는 `app_env`로
띄운 앱은 nano를 부르지 않는다. 진짜 판정기로 띄운 앱은 test_real_guard.py가 본다. 여기서는 가짜
전송 자체와, 튜터→가드 호출까지 있는 판정기 대역(`llm_guard`) 앱을 본다.

- 가드 LLM을 서버 기본 경로(`LLM(cfg.llm)` → `start()`)로 만들어도 실제 클라이언트 대신 가짜로 간다.
- 가짜는 가드 판정 스키마 셋을 두 출력 방식(json_schema · json_object) 모두에서 알아본다.
- 판정기가 nano를 부르는 앱(대역 `llm_guard`)이 뜨고, 두 경로가 규격대로 답하고, 호출 수가 세어지고,
  판정 글과 요청 내용이 응답 · 로그 · 앱 상태에 남지 않는다.
- 키 없이 띄운 루트 앱은 상수 판정기이고 가드 nano를 부르지 않는다(`applib.guard_call_budget` 0).
"""

import asyncio
import importlib
import json
import logging
import time
from typing import Literal

import pytest
from guardrail.config import load_config
from guardrail.guard import ConstantGuard
from guardrail.judges.input_judge import judge_input
from guardrail.judges.schemas import ACT_TO_CATEGORY, InputOut, OutputOut, strict_schema
from guardrail.llm import LLM
from guardrail.schema import Message
from pydantic import BaseModel, ConfigDict

from tests.integration import applib
from tests.integration.applib import (
    ANSWER_WORD,
    AUTH,
    CATEGORIES,
    GUARD_BUDGET_HINT,
    GUARD_MODELS,
    GUARD_WORD,
    MAX_LLM_CALLS_PER_REQUEST,
    UNKNOWN_JSON_OBJECT,
    FakeGuardSend,
    UnknownSchema,
    _guard_reply,
    _guard_schema_name,
    as_bytes,
    assert_guard_contract,
    assert_tutor_contract,
    cache_sizes,
    find_text,
    guard_call_budget,
    guard_calls_per_request,
    patched_guard_llm,
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


def complete_once(llm_factory, schema, *, structured: str | None = None, prompt=PROMPT):
    """LLM을 만들고 start() 뒤 한 번 부른다. (llm, 결과)를 돌려준다."""

    async def run():
        llm = llm_factory()
        await llm.start()
        if structured is not None:
            llm.structured = structured
        result = await llm.complete("input_judge", prompt, schema, deadline=time.monotonic() + 5)
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
# 가짜가 아는 모델 전부(guardrail.judges.schemas의 공개 모델). 가드가 스키마를 더하면 늘어난다
@pytest.mark.parametrize("schema", list(GUARD_MODELS.values()), ids=list(GUARD_MODELS))
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


def test_json_object_output_judge_with_input_fields_in_its_prompt_is_still_output_out(app_env):
    """json_object 방식: 출력 판정 프롬프트 본문에 입력 판정 스키마의 필드 이름이 나와도
    (예시 JSON 등) 가짜는 끝에 붙은 스키마로 판단해 OutputOut으로 답한다."""
    fake = FakeGuardSend()
    prompt = [
        {
            "role": "system",
            "content": '출력 판정. 입력 판정의 예: {"category": "...", "evidence": "..."}',
        },
        {"role": "user", "content": "리스트에 값을 넣는 방법이 뭐야?"},
    ]
    llm, result = complete_once(
        lambda: LLM(load_config().llm, send=fake),
        OutputOut,
        structured="json_object",
        prompt=prompt,
    )
    assert result.error is None and isinstance(result.parsed, OutputOut)
    assert dict(fake.by_schema) == {"OutputOut": 1} and fake.unknown == []
    assert dict(llm.unexpected) == {}


class ReasonGroundedSpansOut(BaseModel):
    """출력 판정 스키마들(OutputBaselineOut의 reason · grounded 등)과 필드가 겹치는 새 스키마
    (가드 스키마 모듈에 없어 가짜가 모르는 것)."""

    reason: str
    grounded: bool
    spans: list[str]


def test_json_object_new_schema_overlapping_output_fields_is_recorded_as_unknown(app_env):
    """json_object 방식: 필드가 겹친다고 아는 스키마로 답하지 않는다. 모르는 스키마로
    `unknown`에 남아(`fake_guard_send` 픽스처라면 끝날 때 실패) 조용한 bad_json이 되지 않는다."""
    fake = FakeGuardSend()
    llm, result = complete_once(
        lambda: LLM(load_config().llm, send=fake),
        ReasonGroundedSpansOut,
        structured="json_object",
    )
    assert result.parsed is None
    assert fake.unknown == [UNKNOWN_JSON_OBJECT]
    assert dict(llm.unexpected) == {UnknownSchema.__name__: 1}


def test_fake_builds_replies_from_the_guard_models_fields():
    """가짜 응답은 가드 모델의 필드에서 만든다. 가드가 필드를 바꾸면(#147 OutputOut) 따라간다."""
    assert {"InputOut", "InputActOut", "OutputOut", "OutputBaselineOut"} <= set(GUARD_MODELS)
    assert all(not name.startswith("_") for name in GUARD_MODELS)
    for name, model in GUARD_MODELS.items():
        reply = _guard_reply(model, "판정 글", iter(range(1000)), vary=False)
        assert reply is not None and list(reply) == list(model.model_fields), name
        model.model_validate(reply)  # 엄격 모델 검증을 통과한다
        for field, value in reply.items():
            if field in ("grounded", "safe"):
                assert value is True, (name, field)  # 튜터 답을 건드리지 않는 쪽
            elif isinstance(value, bool):
                assert value is False, (name, field)
            elif field.endswith("_quote"):
                assert value == "", (name, field)
    assert _guard_reply(GUARD_MODELS["InputOut"], "x", iter(()), vary=False)["category"] == "normal"
    # act도 튜터 답을 건드리지 않는 값이다: 가드 대응표로 normal(첫 값은 low_signal_input)
    act = _guard_reply(GUARD_MODELS["InputActOut"], "x", iter(()), vary=False)["act"]
    assert ACT_TO_CATEGORY[act] == "normal"


class _StrictOut(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ChangedJudgeOut(_StrictOut):
    """필드가 바뀐 판정 스키마의 흉내(가짜의 응답 생성이 옛 모양을 내면 검증에 걸린다)."""

    passage_quote: str
    supported: bool


class ListFieldOut(_StrictOut):
    """가짜가 만들 줄 모르는 타입(list)이 있는 스키마."""

    spans: list[str]


class NewLiteralOut(_StrictOut):
    """기본값 표에 없는 문자열 Literal 필드가 있는 스키마(가드가 새 판정 스키마를 올린 경우)."""

    verdict: Literal["block", "allow"]


@pytest.mark.parametrize("structured", ["json_schema", "json_object"])
def test_fake_reply_that_fails_the_models_validation_is_recorded(app_env, monkeypatch, structured):
    """가짜가 만든 응답이 모델 검증에 실패하면 옛 모양으로 답하지 않고 `unknown`에 남긴다
    (`fake_guard_send` 픽스처라면 끝날 때 실패). LLM 결과는 parsed가 None이다."""
    monkeypatch.setitem(GUARD_MODELS, "ChangedJudgeOut", ChangedJudgeOut)
    # 응답 생성이 가드의 옛 출력 판정 모양을 낸다고 하자(#147 전의 가짜가 하던 것)
    monkeypatch.setattr(applib, "_guard_reply", lambda *a, **k: {"reason": "x", "grounded": True})
    fake = FakeGuardSend()
    llm, result = complete_once(
        lambda: LLM(load_config().llm, send=fake), ChangedJudgeOut, structured=structured
    )
    assert result.parsed is None
    assert fake.unknown == ["ChangedJudgeOut: 가짜 응답이 스키마와 맞지 않음"]
    assert dict(fake.by_schema) == {"ChangedJudgeOut": 1}
    assert dict(llm.unexpected) == {UnknownSchema.__name__: 1}


def test_fake_cannot_build_a_field_type_it_does_not_know(app_env, monkeypatch):
    """bool · str · 문자열 Literal 밖의 타입이 있으면 지어내지 않고 `unknown`에 남긴다."""
    monkeypatch.setitem(GUARD_MODELS, "ListFieldOut", ListFieldOut)
    assert _guard_reply(ListFieldOut, "x", iter(()), vary=False) is None
    fake = FakeGuardSend()
    _, result = complete_once(lambda: LLM(load_config().llm, send=fake), ListFieldOut)
    assert result.parsed is None
    assert fake.unknown == ["ListFieldOut: 가짜 응답이 스키마와 맞지 않음"]


def test_fake_fills_a_literal_field_missing_from_the_defaults_with_its_first_value(
    app_env, monkeypatch
):
    """기본값 표에 없는 문자열 Literal 필드는 첫 값으로 채우고, 응답은 검증을 통과한다.

    가드가 새 판정 스키마(예: 입력 v2 InputModeOut)를 올려도 tests/를 고치지 않고 돈다.
    """
    monkeypatch.setitem(GUARD_MODELS, "NewLiteralOut", NewLiteralOut)
    assert _guard_reply(NewLiteralOut, "x", iter(()), vary=False) == {"verdict": "block"}
    fake = FakeGuardSend()
    _, result = complete_once(lambda: LLM(load_config().llm, send=fake), NewLiteralOut)
    assert result.parsed == NewLiteralOut(verdict="block")
    assert fake.unknown == []


class JbLikeOut(_StrictOut):
    """가드 #183 탈옥 2차 판정(JbCheckOut)과 같은 모양. main에는 아직 없어 여기서 흉내 낸다."""

    evidence: str
    verdict: Literal["apply", "study"]


def test_fake_force_sets_the_field_and_wins_over_vary(app_env):
    """`force`의 값이 그 필드를 정한다(vary보다 먼저). 모델에 없는 필드면 쓰이지 않는다."""
    jailbreak = {"category": "jailbreak_injection"}
    for seed in range(20):  # vary면 범주가 해시로 갈리지만, force가 있으면 늘 그 값이다
        picks = iter([seed] * 50)
        reply = _guard_reply(InputOut, "x", picks, vary=True, force=jailbreak)
        assert reply["category"] == "jailbreak_injection"
    output = _guard_reply(OutputOut, "x", iter(()), vary=False, force=jailbreak)
    assert "category" not in output and output["grounded"] is True
    # 탈옥 2차 판정 모양의 기본값은 study(튜터 답을 건드리지 않는 쪽), force로 apply를 낼 수 있다
    assert _guard_reply(JbLikeOut, "x", iter(()), vary=False)["verdict"] == "study"
    forced = _guard_reply(JbLikeOut, "x", iter(()), vary=False, force={"verdict": "apply"})
    assert forced["verdict"] == "apply"

    fake = FakeGuardSend(vary=True)
    fake.force = dict(jailbreak)
    _, result = complete_once(lambda: LLM(load_config().llm, send=fake), InputOut)
    assert isinstance(result.parsed, InputOut)
    assert result.parsed.category == "jailbreak_injection"
    assert fake.unknown == []


@pytest.mark.parametrize("structured", ["json_schema", "json_object"])
def test_fake_force_value_outside_the_schema_is_recorded(app_env, structured):
    """스키마에 없는 값을 force하면 지어내지 않고 검증 실패로 `unknown`에 남는다."""
    fake = FakeGuardSend()
    fake.force = {"category": "탈옥"}
    llm, result = complete_once(
        lambda: LLM(load_config().llm, send=fake), InputOut, structured=structured
    )
    assert result.parsed is None
    assert fake.unknown == ["InputOut: 가짜 응답이 스키마와 맞지 않음"]
    assert dict(llm.unexpected) == {UnknownSchema.__name__: 1}


def test_fake_internal_error_is_recorded(app_env, monkeypatch):
    """가짜 안에서 UnknownSchema가 아닌 예외가 나도 `unknown`에 종류 이름을 남긴다(가드 LLM이
    전송 예외를 삼키므로, 남기지 않으면 테스트가 조용히 지나간다)."""

    def broken(*args, **kwargs):
        raise KeyError("가짜 안의 실수")

    monkeypatch.setattr(applib, "_guard_reply", broken)
    fake = FakeGuardSend()
    llm, result = complete_once(lambda: LLM(load_config().llm, send=fake), InputOut)
    assert result.parsed is None
    assert fake.unknown == ["가짜 내부 오류: KeyError"]
    assert dict(llm.unexpected) == {KeyError.__name__: 1}


def test_guard_schema_name_reads_only_the_appended_schema():
    """json_object 방식의 스키마 판별: system 끝 표식 뒤의 JSON이 아는 strict 스키마와 정확히 같을
    때만 그 이름이다. 표식이 없거나, JSON이 아니거나, 하나라도 다르면 모른다("")."""
    note = "JSON 객체만 출력한다. 스키마:\n"
    plain = {"response_format": {"type": "json_object"}}

    def name(system: str) -> str:
        return _guard_schema_name(plain, system)

    assert {"InputOut", "OutputOut", "OutputBaselineOut"} <= set(GUARD_MODELS)
    for schema in GUARD_MODELS.values():
        dumped = json.dumps(strict_schema(schema), ensure_ascii=False)
        assert name(f"판정\n\n{note}{dumped}") == schema.__name__
        # 본문에 다른 스키마(표식 포함)가 먼저 나와도 마지막 표식 뒤만 본다
        other = json.dumps(strict_schema(InputOut), ensure_ascii=False)
        assert name(f"예시 {note}{other}\n\n{note}{dumped}") == schema.__name__
    assert name('{"category": "normal", "evidence": "x"}') == ""  # 표식 없음
    assert name(f"판정\n\n{note}{{깨진 JSON") == ""
    changed = strict_schema(OutputOut)
    changed["properties"]["spans"] = {"type": "array", "items": {"type": "string"}}
    assert name(f"{note}{json.dumps(changed, ensure_ascii=False)}") == ""  # 필드 하나 더
    assert name(f"{note}{json.dumps(strict_schema(ReasonGroundedSpansOut))}") == ""
    # json_schema 방식은 이름으로 본다
    named = {"response_format": {"type": "json_schema", "json_schema": {"name": "OutputOut"}}}
    assert _guard_schema_name(named, "아무 글") == "OutputOut"


def test_fixture_teardown_fails_when_the_fake_saw_an_unknown_schema():
    """`fake_guard_send` 픽스처의 정리 단계(`applib.patched_guard_llm`)가 `unknown`이 비지 않으면
    실패를 낸다. 픽스처를 쓰는 테스트로는 정리 단계의 실패를 볼 수 없어 제너레이터를 직접 돌린다."""
    env = {"OPENAI_API_KEY": "k", "OPENAI_MODEL": "m", "OPENAI_BASE_URL": "http://127.0.0.1:9/v1"}
    llm_mod = importlib.import_module("guardrail.llm")
    real_openai, real_env = llm_mod.openai, llm_mod.openai_env

    # unknown이 비어 있으면 정리 단계가 조용히 끝난다
    with pytest.MonkeyPatch.context() as mp:
        steps = patched_guard_llm(mp, env)
        fake = next(steps)
        assert isinstance(fake, FakeGuardSend) and llm_mod.openai_env() == env
        with pytest.raises(StopIteration):
            next(steps)

    # unknown에 값이 있으면 정리 단계에서 AssertionError
    with pytest.MonkeyPatch.context() as mp:
        steps = patched_guard_llm(mp, env)
        fake = next(steps)
        fake.unknown.append("NewJudgeOut")
        with pytest.raises(AssertionError, match="FakeGuardSend"):
            next(steps)

    # 패치는 MonkeyPatch 범위 안에서만 걸렸다
    assert (llm_mod.openai, llm_mod.openai_env) == (real_openai, real_env)


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
        # 대역 판정기에는 classify · judge_output이 있어 튜터가 둘 다 맡긴다(범주를 넘기므로
        # judge_output은 classify를 다시 부르지 않는다). 건너뛴 단계가 없다
        assert fake_guard_send.count - before == 2
        assert dict(client.app.state.guard_skips) == {}

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


def test_root_app_without_a_key_uses_the_constant_judge(client):
    # 키가 없으면 상수 판정기다(`default_factory`). 아래 상한 0과 다른 통합 테스트가 이 전제에 선다
    assert isinstance(client.app.state.guard, ConstantGuard), type(client.app.state.guard)


def test_root_app_guard_nano_calls_stay_within_the_budget(client, fake_send, fake_guard_send):
    bodies = [
        guard_body(QUESTION),
        guard_body(QUESTION, candidate=CANDIDATE),
        guard_body("ㅋㅋㅋ"),
        guard_body(QUESTION, candidate=""),
    ]
    for sent, body in enumerate(bodies, start=1):
        before = fake_guard_send.count
        assert post(client, "/guard", body).status_code == 200
        # 요청마다 본다: 합계만 보면 한 요청이 넘어도 다른 요청이 적게 쓰면 통과한다
        used = fake_guard_send.count - before
        assert used <= guard_calls_per_request("/guard"), GUARD_BUDGET_HINT
        assert fake_guard_send.count <= guard_call_budget(guard=sent), GUARD_BUDGET_HINT
    assert fake_send.count == 0  # 가드는 튜터 전송을 쓰지 않는다
