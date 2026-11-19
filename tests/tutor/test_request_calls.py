"""요청당 nano 시도 상한(`llm.request_max_calls`): 설정 계산·로드 검증·보내기 직전 예약.

LLM 전송은 가짜다(API 0회).
"""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError

from tutor.runtime import config
from tutor.runtime.llm import REQUEST_QUOTA, RequestQuota, TutorLLM


class Out(BaseModel):
    ok: bool


def calls(**overrides):
    return config.request_calls(config.load_config(overrides=overrides))


def test_repo_config_counts_the_hedge_and_fits_the_limit():
    cfg = config.load_config(config.ROOT / config.DEFAULT_CONFIG)
    normal, worst = config.request_calls(cfg)
    # 주 호출 1 + T 1 + E 1 / 최악은 재시도 1 + 겹쳐 보낸 주 호출 1을 더한다
    assert (normal, worst) == (3, 5)
    assert worst <= cfg.llm.request_max_calls <= config.REQUEST_CALLS_CEILING


def test_hedge_counts_only_when_it_is_on():
    on = calls(**{"llm.timeout_s.main_hedge": 12, "llm.timeout_s.main": 18})
    off = calls(**{"llm.timeout_s.main_hedge": 18, "llm.timeout_s.main": 18})
    assert on[1] == off[1] + 1 and on[0] == off[0]


def test_baseline_mode_is_one_call_with_one_retry():
    assert calls(mode="baseline") == (1, 2)


def test_a_config_whose_worst_case_exceeds_the_limit_is_rejected():
    heavy = {
        "toggles.intent_override": "parallel",
        "toggles.guard_classify": "parallel",
        "toggles.verify": "guard",
        "toggles.oom_gate": "all",
        "llm.timeout_s.main_hedge": 12,
    }
    # 주 호출 1(T·classify를 함께 켜면 재시도 0) + 겹쳐 보내기 1 + T 1 + classify 2 + judge_output 1
    # + E 1 = 7
    assert calls(**heavy)[1] == 7
    with pytest.raises(ValidationError, match="request_max_calls"):
        config.load_config(overrides={**heavy, "llm.request_max_calls": 6})


@pytest.mark.parametrize("value", [0, 9])
def test_request_limit_stays_within_the_ceiling(value):
    with pytest.raises(ValidationError):
        config.load_config(overrides={"llm.request_max_calls": value})


# --- 보내기 직전 예약 ---


def llm_with(send):
    @asynccontextmanager_free
    def gate():
        return _Null()

    base = SimpleNamespace(params={}, structured="json_schema")
    return TutorLLM(send, gate, model="m", base=base)


class _Null:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


def asynccontextmanager_free(fn):  # 게이트는 열린 그대로(테스트용)
    return fn


def reply():
    choice = SimpleNamespace(
        finish_reason="stop", message=SimpleNamespace(content='{"ok": true}', refusal=None)
    )
    return SimpleNamespace(choices=[choice], usage=SimpleNamespace(completion_tokens=5))


def test_parallel_attempts_cannot_pass_the_request_limit_together():
    sent = []

    async def send(kwargs):
        sent.append(1)
        await asyncio.sleep(0.01)
        return reply()

    llm = llm_with(send)

    async def go():
        REQUEST_QUOTA.set(RequestQuota(2))
        deadline = asyncio.get_running_loop().time() + 5
        jobs = [
            asyncio.ensure_future(
                llm.complete(f"r{i}", [], Out, deadline=deadline, timeout_s=1, retries=0)
            )
            for i in range(5)
        ]
        return await asyncio.gather(*jobs)

    results = asyncio.run(go())
    assert len(sent) == 2  # 다섯 작업이 동시에 와도 둘만 보낸다
    assert sorted(r.error or "ok" for r in results) == ["budget", "budget", "budget", "ok", "ok"]
    assert sum(llm.capped.values()) == 3 and not llm.blocked  # 하네스 예산 막힘과 따로 센다


def test_without_a_request_quota_nothing_is_capped():
    sent = []

    async def send(kwargs):
        sent.append(1)
        return reply()

    llm = llm_with(send)

    async def go():
        deadline = asyncio.get_running_loop().time() + 5
        return [
            await llm.complete("main", [], Out, deadline=deadline, timeout_s=1, retries=0)
            for _ in range(10)
        ]

    assert all(r.error is None for r in asyncio.run(go()))
    assert len(sent) == 10


def test_a_capped_retry_keeps_the_earlier_error():
    async def send(kwargs):
        raise TimeoutError

    llm = llm_with(send)

    async def go():
        REQUEST_QUOTA.set(RequestQuota(1))
        deadline = asyncio.get_running_loop().time() + 50
        return await llm.complete("main", [], Out, deadline=deadline, timeout_s=1, retries=1)

    llm._rand = lambda: 0.0  # 재시도 지터 0.2초
    result = asyncio.run(go())
    assert result.error == "timeout" and result.attempts == 1  # 재시도는 상한에 막혔다
    assert llm.capped["main"] == 1


def test_pipeline_gives_each_request_its_own_quota(monkeypatch):
    from tutor import pipeline

    seen = []

    async def fake_answer(req, *, deadline, state):
        quota = REQUEST_QUOTA.get()
        seen.append((quota.limit, quota.used))
        quota.take()
        return {"answer": "a", "action": "answer", "citations": []}

    monkeypatch.setattr(pipeline, "_answer", fake_answer)
    state = SimpleNamespace(tutor_cfg=config.load_config())
    body = {"messages": [{"role": "user", "content": "질문"}]}

    async def go():
        for _ in range(2):
            await pipeline.respond(body, deadline=1e12, state=state)
        return REQUEST_QUOTA.get()

    assert asyncio.run(go()) is None  # 요청이 끝나면 걸어 둔 상한을 거둔다
    limit = state.tutor_cfg.llm.request_max_calls
    assert seen == [(limit, 0), (limit, 0)]  # 요청마다 새로 센다


def test_hedged_and_retried_main_call_is_counted_by_the_real_llm():
    """겹쳐 보낸 주 호출과 처음 주 호출의 재시도가 함께 나가도 예약 수 = 보낸 시도 수 ≤ 최악 계산."""
    import httpx
    import openai

    from tests.tutor.test_flow import state_with
    from tutor.judge import flow

    req = httpx.Request("POST", "http://fake.invalid/v1/chat/completions")
    order = []

    async def send(kwargs):
        index = len(order)
        order.append(index)
        if index == 0:  # 처음 주 호출: 겹쳐 보내기 시간이 지난 뒤 500(재시도 대상)
            await asyncio.sleep(0.1)
            raise openai.APIStatusError("x", response=httpx.Response(500, request=req), body=None)
        if index == 1:  # 겹쳐 보낸 주 호출: 늦게 실패
            await asyncio.sleep(0.6)
            raise openai.APIStatusError("x", response=httpx.Response(500, request=req), body=None)
        return reply()  # 처음 주 호출의 재시도: 성공

    llm = llm_with(send)
    llm._rand = lambda: 0.0  # 재시도 지터 0.2초
    over = {"llm.timeout_s.main_hedge": 0.05, "llm.timeout_s.main": 1.0}
    state = state_with(llm, **over)
    complete = flow._complete(state)

    async def go():
        quota = RequestQuota(state.tutor_cfg.llm.request_max_calls)
        REQUEST_QUOTA.set(quota)
        deadline = asyncio.get_running_loop().time() + 10
        got = await complete([], Out, deadline=deadline)
        await asyncio.sleep(0)
        return got, quota

    got, quota = asyncio.run(go())
    assert got.error is None
    assert dict(llm.calls) == {"main": 2, "main_hedge": 1}
    assert quota.used == sum(llm.calls.values()) == 3
    # 이 설정의 최악 계산(주 호출 1 + 재시도 1 + 겹쳐 보내기 1)이 실제로 나간 수를 덮는다
    main_only = config.request_calls(state.tutor_cfg)
    assert main_only == (1, 3) and quota.used <= main_only[1]
