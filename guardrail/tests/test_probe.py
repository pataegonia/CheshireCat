"""S1.6 probe: 집계·예상 호출 수·상한 (C1 §S1.6). FakeLLM으로 0ms, 실제 API는 쓰지 않는다."""

import json

import pytest
from fakes import FakeClock, FakeLLM, Ok, RateLimited, Timeout

from guardrail.config import load_config
from guardrail.data import PublicRow
from guardrail.eval import probe
from guardrail.llm import LLM, LLMResult
from guardrail.schema import Message

pytestmark = pytest.mark.anyio

CFG = load_config()
BODY_MARK = "비밀본문-절대출력금지"  # 이 문자열이 보고에 나오면 본문이 샌 것


def _row(i: int, answer: str | None = "답변") -> PublicRow:
    msgs = tuple(
        Message("user" if k % 2 == 0 else "assistant", f"{BODY_MARK} {i}-{k}") for k in range(7)
    )
    return PublicRow(f"g{i}", "guard", msgs, answer, {"category": "normal"})


ROWS = [_row(i) for i in range(3)] + [_row(9, answer=None)]


def _res(ms: float, error=None, finish="stop", tokens=10) -> LLMResult:
    return LLMResult(None, error, 1, ms, finish, tokens)


@pytest.fixture(autouse=True)
def _model_env(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5-nano")


def test_pct_small_and_empty():
    assert probe.pct([], 50) is None
    assert probe.pct([7.0], 95) == 7.0
    assert probe.pct([float(x) for x in range(1, 101)], 50) == pytest.approx(50.5)
    assert probe.pct([float(x) for x in range(1, 101)], 95) == pytest.approx(95.05)


def test_summarize_counts_codes_length_tokens():
    rs = [
        _res(100),
        _res(300, finish="length", tokens=800),
        _res(200, error="timeout", finish=None, tokens=None),
        _res(400, error="rate_limit", finish=None, tokens=None),
    ]
    s = probe.summarize(rs)
    assert s["n"] == 4 and s["ok"] == 2
    assert s["errors"] == {"timeout": 1, "rate_limit": 1}
    assert s["length_rate"] == 0.25
    assert s["p50_ms"] == pytest.approx(250.0)
    assert s["tokens_p50"] == pytest.approx(405.0)
    assert s["tokens_max"] == 800


def test_estimate_default_is_189_and_under_cap():
    est = probe.estimate(CFG.llm)
    assert est == {"params": 5, "length": 72, "latency": 112, "total": 189}
    assert est["total"] <= probe.HARD_CAP


def test_estimate_drops_disabled_params():
    llm = CFG.llm.model_copy(update={"verbosity": None, "structured_output": "json_object"})
    assert probe.estimate(llm)["params"] == 3


def test_main_estimate_makes_no_call(capsys, monkeypatch):
    monkeypatch.setattr(probe, "LLM", None)  # 만들면 TypeError
    assert probe.main(["--estimate"]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 189


@pytest.mark.parametrize("calls", ["100", "201"])
def test_main_refuses_over_cap(calls, monkeypatch):
    monkeypatch.setattr(probe, "LLM", None)
    assert probe.main(["--calls", calls]) == 2


def _respond(kwargs):
    rf = kwargs.get("response_format")
    if rf is None or rf["json_schema"]["name"] == "OutputOut":
        body = '{"reason": "r", "grounded": true}'
    else:
        body = '{"evidence": "e", "category": "normal"}'
    finish = "length" if kwargs.get("max_completion_tokens") == 800 else "stop"
    return Ok(content=body, finish_reason=finish, completion_tokens=50)


async def _run(fake: FakeLLM, cap: int = 200) -> dict:
    clock = FakeClock()
    llm = LLM(CFG.llm, send=fake.send, clock=clock, sleep=clock.sleep)
    return await probe.run(llm, ROWS, budget_s=CFG.budget_s, cap=cap, clock=clock)


async def test_run_full_plan_counts_and_no_body():
    fake = FakeLLM(respond=_respond)
    rep = await _run(fake)
    assert len(fake.requests) == 189
    assert rep["calls_total"] == 189
    assert rep["params"]["probe"] == {
        "base": None,
        "reasoning_effort": None,
        "verbosity": None,
        "max_completion_tokens": None,
        "response_format": None,
    }
    length = rep["length"]
    assert length["provisional"] is True
    assert length["800"]["InputOut"]["length_rate"] == 1.0
    assert length["1600"]["OutputOut"]["length_rate"] == 0.0
    assert [c["concurrency"] for c in rep["latency"]] == [1, 16, 64]
    assert [c["n"] for c in rep["latency"]] == [16, 32, 64]
    assert BODY_MARK not in json.dumps(rep, ensure_ascii=False)


async def test_run_stand_in_prompt_shape():
    fake = FakeLLM(respond=_respond)
    await _run(fake)
    judged = [r for r in fake.requests[5:] if "response_format" in r]
    first = judged[0]["messages"]
    assert first[0]["role"] == "system"
    assert first[0]["content"].count("\n- ") == 8  # 예시 8줄
    assert first[-1]["content"].count("<msg ") == 6  # 메시지 6개
    out = [r for r in judged if r["response_format"]["json_schema"]["name"] == "OutputOut"]
    assert all("<answer>" in r["messages"][-1]["content"] for r in out)


async def test_run_skips_length_when_param_rejected():
    from fakes import BadRequest

    # probe 순서: base, reasoning_effort, verbosity, max_completion_tokens(거절), response_format
    fake = FakeLLM(Ok(), Ok(), Ok(), BadRequest(), Ok(), respond=_respond)
    rep = await _run(fake)
    assert rep["params"]["probe"]["max_completion_tokens"] == "bad_request"
    assert rep["length"]["skipped"] == "max_completion_tokens_rejected"
    assert rep["calls_total"] == 5 + 112


async def test_run_never_exceeds_cap_with_retries():
    # 모두 재시도 대상 실패 → 재시도까지 합쳐도 cap에서 멈추고 나머지는 budget
    fake = FakeLLM(Ok(), Ok(), Ok(), Ok(), Ok(), respond=lambda _: RateLimited("0"))
    rep = await _run(fake, cap=200)
    assert len(fake.requests) <= 200
    assert rep["calls_total"] == len(fake.requests)
    codes = rep["latency"][-1]["errors"]
    assert codes.get("budget", 0) > 0


async def test_run_records_error_codes():
    fake = FakeLLM(Ok(), Ok(), Ok(), Ok(), Ok(), respond=lambda _: Timeout())
    rep = await _run(fake)
    assert set(rep["latency"][0]["errors"]) <= {"timeout", "budget"}
    assert rep["latency"][0]["ok"] == 0
