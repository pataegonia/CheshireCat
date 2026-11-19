"""한 요청 안 질의 임베딩 나눠 쓰기(`toggles.share_query_embedding`, `RequestEmbeddings`).

임베딩은 가짜다(API 0회).
"""

import asyncio

import pytest

from tests.tutor.test_embedding import FakeEmbed, bag, build
from tutor.retrieval import embedding as emb


def run(coro):
    return asyncio.run(coro)


def search_with(fake, timeout_s=1.0):
    import dataclasses

    return dataclasses.replace(build(), embed=fake, timeout_s=timeout_s)


def test_same_query_in_one_request_is_embedded_once_with_the_same_ranking():
    fake = FakeEmbed()
    base = search_with(fake)

    async def go():
        alone = await base.rerank("bank card", ["B-01"], allowed=None)
        req = base.for_request()
        first = await req.rerank("bank card", ["B-01"], allowed=None)
        again = await req.rerank("bank card", ["B-01"], allowed=None, embedding_only=True)
        other = await req.rerank("apple basket", ["A-01"], allowed=None)
        req.shared.close()
        return alone, first, again, other

    alone, first, again, other = run(go())
    assert first == alone  # 나눠 써도 순위가 같다
    assert again == base.ranking(bag("bank card"))
    assert other == emb.rrf(["A-01"], base.ranking(bag("apple basket")))
    # 나누지 않은 1회 + 요청 안 "bank card" 1회 + "apple basket" 1회
    assert fake.calls == [["bank card"], ["bank card"], ["apple basket"]]


def test_concurrent_consumers_share_one_call():
    fake = FakeEmbed(delay=0.05)
    req = search_with(fake).for_request()

    async def go():
        got = await asyncio.gather(
            req.rerank("bank card", ["B-01"], allowed=None),
            req.rerank("bank card", ["B-01"], allowed=None),
        )
        req.shared.close()
        return got

    a, b = run(go())
    assert a == b and len(fake.calls) == 1


def test_requests_do_not_share():
    fake = FakeEmbed()
    base = search_with(fake)

    async def go():
        for _ in range(2):
            req = base.for_request()
            await req.rerank("bank card", ["B-01"], allowed=None)
            req.shared.close()

    run(go())
    assert len(fake.calls) == 2  # 요청 사이에는 나누지 않는다(TU-18)


def test_a_failed_call_is_not_reused():
    class FailOnce(FakeEmbed):
        async def __call__(self, texts):
            self.calls.append(list(texts))
            if len(self.calls) == 1:
                raise RuntimeError("일시 오류")
            return [bag(t) for t in texts]

    fake = FailOnce()
    req = search_with(fake).for_request()

    async def go():
        first = await req.rerank("bank card", ["B-01"], allowed=None)
        second = await req.rerank("bank card", ["B-01"], allowed=None)
        req.shared.close()
        return first, second

    first, second = run(go())
    assert first == ["B-01"]  # 실패는 BM25
    assert second == emb.rrf(["B-01"], search_with(FakeEmbed()).ranking(bag("bank card")))
    assert len(fake.calls) == 2  # 실패한 결과를 다시 쓰지 않고 새로 불렀다


def test_one_consumer_timing_out_does_not_cancel_the_other():
    fake = FakeEmbed(delay=0.1)
    base = search_with(fake)
    req = base.for_request()

    async def go():
        short = __import__("dataclasses").replace(req, timeout_s=0.02)
        quick, slow = await asyncio.gather(
            short.rerank("bank card", ["B-01"], allowed=None),
            req.rerank("bank card", ["B-01"], allowed=None),
        )
        req.shared.close()
        return quick, slow

    quick, slow = run(go())
    assert quick == ["B-01"]  # 짧은 쪽은 시간 초과로 BM25
    assert slow == emb.rrf(["B-01"], base.ranking(bag("bank card")))  # 긴 쪽은 같은 호출로 받는다
    assert len(fake.calls) == 1


def test_the_call_is_cancelled_when_nobody_waits_any_more():
    started, cancelled = [], []

    async def slow(texts):
        started.append(1)
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled.append(1)
            raise
        return [bag(t) for t in texts]

    req = search_with(slow, timeout_s=0.02).for_request()

    async def go():
        got = await req.rerank("bank card", ["B-01"], allowed=None)
        await asyncio.sleep(0.01)
        return got

    assert run(go()) == ["B-01"]
    assert started == [1] and cancelled == [1]  # 나누지 않을 때처럼 시간 초과가 호출을 접는다


def test_close_cancels_pending_calls_and_outer_cancel_propagates():
    fake = FakeEmbed(delay=1.0)
    req = search_with(fake, timeout_s=5).for_request()

    async def go():
        task = asyncio.ensure_future(req.rerank("bank card", ["B-01"], allowed=None))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        req.shared.close()
        return req.shared._jobs

    assert run(go()) == {}


# --- 흐름: E·주 호출·규칙 A ---


def test_flow_embeds_the_same_query_once_per_request_when_sharing_is_on():
    from tests.tutor.test_english_embedding import EN_Q, ON, fake_search, with_embedding
    from tests.tutor.test_flow import state_with
    from tests.tutor.test_oom_gate import ABSTAIN, RoleLLM, _ids, out
    from tests.tutor.test_oom_gate import run as run_flow
    from tutor.judge import oom_gate

    calls = []
    llm = RoleLLM(out(), found=False)
    search = fake_search(lambda text: "PY-01-01", calls)
    share = {**ON, "toggles.share_query_embedding": True}
    state = with_embedding(state_with(llm, **share), search)
    assert run_flow(state, EN_Q) == ABSTAIN  # 나눠 써도 E 판정 경로는 같다
    gate_user = llm.kwargs[oom_gate.ROLE]["messages"][1]["content"]
    main_user = llm.kwargs["main"]["messages"][-1]["content"]
    assert _ids(gate_user)[0] == "PY-01-01" and _ids(main_user) == _ids(gate_user)
    assert len(calls) == 1  # 끄면 E 묶음 + 주 호출 + 규칙 A로 3회(test_english_embedding)
    run_flow(state, EN_Q)
    assert len(calls) == 2  # 다음 요청은 새로 부른다(요청 사이에는 나누지 않는다)


def test_flow_still_embeds_a_different_rule_a_query():
    from tests.tutor.test_english_embedding import DIALOG, ROLES, fake_search, with_embedding
    from tests.tutor.test_flow import FakeLLM, ask, out, state_with

    calls = []
    search = fake_search(lambda text: "PY-03-04" if "zeta" in text else "PY-01-01", calls)
    toggles = {
        "toggles.english_embedding": True,
        "toggles.cite_top1": True,
        "toggles.top1_dialog_query": True,
        "toggles.oom_gate": "named_term",
        "toggles.share_query_embedding": True,
    }
    state = with_embedding(state_with(FakeLLM(out()), **toggles), search)
    resp = ask(state, *DIALOG, roles=ROLES)
    assert resp["citations"] == ["PY-01-01", "PY-03-04"]  # 끈 때(test_english_embedding)와 같다
    # E·주 호출은 같은 질의(학습자 발화) 1회, 규칙 A는 튜터 발화를 넣은 다른 질의 1회
    assert len(calls) == 2 and calls[0] != calls[1]
