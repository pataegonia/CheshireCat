"""S0.4 trace: 키 누출 0, 평가/서비스 모드 원문 필드, 단계 지연, 예외 단계."""

import json
import secrets

import httpx2
import pytest
from fakes import FakeClock, FakeLLM, ServerError, fake_config
from openai import AsyncOpenAI

from guardrail.llm import LLM, openai_send
from guardrail.pipeline import _run_stage
from guardrail.trace import SECRET_ENV_EXACT, SECRET_ENV_PREFIXES, TraceWriter, make_record
from guardrail.types import Session, StageResult, Verdict

pytestmark = pytest.mark.anyio

SECRET_VARS = (
    "GUARD_LLM_BASE_URL",
    "GUARD_LLM_API_KEY",
    "JEV_BASE_URL",
    "JEV_API_KEY",
    "GUARD_LOG_HMAC_KEY",
    "TUTOR_API_TOKEN",  # 임의의 TUTOR_* 하나
)


def _stage(name, **kw) -> StageResult:
    return StageResult(name, None, None, None, None, kw.pop("reason_code", "x"), 1.0, **kw)


def _verdict(stages=None, reason="", response="답 [S1]") -> Verdict:
    return Verdict(
        verdict="pass",
        action="allow",
        category="S0",
        confidence=None,
        rule_id=None,
        reason_code="stages_off",
        reason=reason,
        latency_ms=12.5,
        policy_version="0.1.0",
        request_id="uuid-from-guard",
        response=response,
        stages=stages or [],
    )


def _read(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def service_cfg(monkeypatch):
    monkeypatch.setenv("GUARD_LOG_HMAC_KEY", secrets.token_hex(12))
    return fake_config(mode="service", **{"stages.crisis": True, "stages.input.pii": True})


# --- 레코드 모양 ---


def test_eval_record_fields():
    cfg = fake_config()
    stages = [
        _stage("input_judge", tokens_in=30, tokens_out=7, model="fake-input_judge"),
        _stage("generate", tokens_in=100, tokens_out=50, detail={"chars": 9}),
        _stage("retrieve", tokens_in=5),
    ]
    rec = make_record(
        "smk-ben-0001#0", _verdict(stages), cfg, input_raw="질문", displayed_output="답"
    )
    assert set(rec) == {
        "request_id", "ts", "policy_version", "config_hash", "mode", "final_action", "verdict",
        "crisis_tier", "fallback_infra", "latency_ms", "tokens_in", "tokens_out", "stages",
        "input_raw", "displayed_output",
    }  # fmt: skip
    assert rec["request_id"] == "smk-ben-0001#0"  # 평가 기록은 하네스의 평가 id
    assert (rec["mode"], rec["final_action"], rec["verdict"]) == ("eval", "allow", "pass")
    assert (rec["tokens_in"], rec["tokens_out"]) == (30, 7)  # 튜터 단계 토큰은 빼고 센다
    assert rec["stages"][0]["model"] == "fake-input_judge"
    assert rec["ts"].endswith("+00:00") and "." in rec["ts"]
    assert len(rec["config_hash"]) == 12


def test_service_record_has_no_raw_fields_or_canary(service_cfg):
    stages = [_stage("output_code", detail={"canary": "CANARY-123", "labels": [1]})]
    rec = make_record("r", _verdict(stages), service_cfg, input_raw="원문", displayed_output="출력")
    assert "input_raw" not in rec and "displayed_output" not in rec
    assert rec["stages"][0]["detail"] == {"labels": [1]}
    assert rec["mode"] == "service"


def test_eval_record_keeps_canary_detail():
    stages = [_stage("output_code", detail={"canary": "CANARY-123"})]
    rec = make_record("r", _verdict(stages), fake_config())
    assert rec["stages"][0]["detail"] == {"canary": "CANARY-123"}


def test_make_record_does_not_mutate_verdict(service_cfg):
    stages = [_stage("output_code", detail={"canary": "C"})]
    v = _verdict(stages)
    make_record("r", v, service_cfg)
    assert v.stages[0].detail == {"canary": "C"}


def test_eval_mode_ignores_session():
    cfg = fake_config()
    plain = make_record("r", _verdict(), cfg)
    with_session = make_record(
        "r", _verdict(), cfg, session=Session(user_id="u1", grade_band="중1")
    )
    plain.pop("ts")
    with_session.pop("ts")
    assert plain == with_session
    assert not {"user_id", "user_hmac", "grade_band"} & set(with_session)


def test_input_request_id_only_when_given(service_cfg):
    assert "input_request_id" not in make_record("r", _verdict(), service_cfg)
    rec = make_record("r", _verdict(), service_cfg, input_request_id="in-1")
    assert rec["input_request_id"] == "in-1"


# --- 키 누출 0 ---


def test_secret_env_list_covers_contract():
    for name in SECRET_VARS:
        assert name in SECRET_ENV_EXACT or name.startswith(SECRET_ENV_PREFIXES)
    assert not "GEN_MODEL".startswith(SECRET_ENV_PREFIXES) and "GEN_MODEL" not in SECRET_ENV_EXACT


def test_no_secret_values_in_trace(tmp_path, monkeypatch):
    # 값은 런타임에 만든다. 정규식 밖 모양이라 환경변수 수집이 깨지면 실패한다
    values = {name: secrets.token_hex(12) for name in SECRET_VARS}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("GEN_MODEL", "gen-model-visible")
    regex_only = "sk-" + "x" * 20
    joined = " ".join(values.values())
    stages = [
        _stage(
            "input_judge", error=f"auth {values['GUARD_LLM_API_KEY']}", model="gen-model-visible"
        ),
        _stage("generate", detail={"chars": 3}),
    ]
    v = _verdict(stages, reason=f"Authorization: Bearer {values['JEV_API_KEY']} {regex_only}")
    rec = make_record(
        "r#0", v, fake_config(), input_raw=f'{joined} 큰따옴표" 역슬래시\\', displayed_output="ok"
    )
    path = tmp_path / "trace.jsonl"
    with TraceWriter(path) as w:
        w.write(rec)
    text = path.read_text(encoding="utf-8")
    for value in values.values():
        assert value not in text
    assert regex_only not in text and "Bearer" not in text.replace("[REDACTED]", "")
    assert "gen-model-visible" in text
    assert _read(path)[0]["request_id"] == "r#0"  # 가린 뒤에도 JSON으로 읽힌다


def test_secret_with_json_escapes_is_redacted(tmp_path, monkeypatch):
    tricky = 'ab"cd\\ef' + secrets.token_hex(4)
    monkeypatch.setenv("TUTOR_WEIRD", tricky)
    path = tmp_path / "t.jsonl"
    with TraceWriter(path) as w:
        w.write({"x": tricky})
    assert _read(path) == [{"x": "[REDACTED]"}]


def test_short_env_values_are_not_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("TUTOR_FLAG", "on")  # 8자 미만은 흔한 글자를 지우지 않게 뺀다
    path = tmp_path / "t.jsonl"
    with TraceWriter(path) as w:
        w.write({"x": "turn on"})
    assert _read(path) == [{"x": "turn on"}]


async def test_server_error_and_auth_header_never_reach_result_or_trace(tmp_path, monkeypatch):
    key = "test-" + secrets.token_hex(12)
    monkeypatch.setenv("GUARD_LLM_API_KEY", key)
    seen_auth: list[str] = []

    def handler(request):
        seen_auth.append(request.headers["authorization"])
        return httpx2.Response(
            500, json={"error": {"message": f"echo {request.headers['authorization']}"}}
        )

    client = AsyncOpenAI(
        base_url="http://test",
        api_key=key,
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    clock = FakeClock()
    llm = LLM(fake_config(), send=openai_send(client), clock=clock.now, sleep=clock.sleep)
    res = await llm.complete("input_judge", [{"role": "user", "content": "q"}], deadline_s=30.0)
    assert res.error == "server" and seen_auth and key in seen_auth[0]
    assert key not in repr(res)

    fake = FakeLLM(script=[ServerError()] * 3)
    res2 = await fake.complete("input_judge", [{"role": "user", "content": "q"}], deadline_s=30.0)
    stage = _stage("input_judge", status="error", error=res.error, attempts=res.attempts)
    stage2 = _stage("exchange_judge", status="error", error=res2.error)
    path = tmp_path / "trace.jsonl"
    with TraceWriter(path) as w:
        w.write(make_record("r#0", _verdict([stage, stage2]), fake_config()))
    text = path.read_text(encoding="utf-8")
    assert key not in text and "authorization" not in text.lower()
    assert '"error": "server"' in text


def test_writer_appends_and_flushes(tmp_path):
    path = tmp_path / "sub" / "trace.jsonl"
    w = TraceWriter(path)
    w.write({"a": 1})
    assert _read(path) == [{"a": 1}]  # 닫기 전에도 디스크에 있다
    w.write({"a": "한글"})
    w.close()
    assert '"한글"' in path.read_text(encoding="utf-8")  # ensure_ascii=False


# --- _run_stage ---


async def test_run_stage_measures_latency():
    clock = FakeClock(start=0.0)

    def work():
        clock.advance(0.010)
        return _stage("rules", reason_code="no_match")

    res = await _run_stage("rules", work, t0=0.0, clock=clock.now)
    assert res.latency_ms >= 10 and res.status == "ok" and res.reason_code == "no_match"
    assert res.started_ms == 0.0


async def test_run_stage_async_fn_and_started_ms():
    clock = FakeClock(start=5.0)

    async def work():
        clock.advance(0.002)
        return _stage("input_judge")

    res = await _run_stage("input_judge", work, t0=4.0, clock=clock.now)
    assert res.started_ms == pytest.approx(1000.0) and res.latency_ms == pytest.approx(2.0)


async def test_run_stage_exception_has_class_name_only():
    secret = secrets.token_hex(12)

    def boom():
        raise RuntimeError(f"key={secret}")

    res = await _run_stage("rules", boom, t0=0.0)
    assert (res.status, res.error, res.reason_code) == ("error", "RuntimeError", "stage_exception")
    assert res.stage == "rules" and res.action is None
    assert secret not in repr(res)


async def test_run_stage_does_not_swallow_cancel():
    import asyncio

    async def cancelled():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _run_stage("input_judge", cancelled, t0=0.0)
