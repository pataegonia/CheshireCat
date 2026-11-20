"""handle 계약(TU-01): 어떤 입력에도 예외 없이 튜터 규격 응답. 요청 사이 상태 없음(TU-18)."""

import asyncio
import inspect
import time

from hypothesis import given, settings
from hypothesis import strategies as st

import tutor
from tests.tutor.tutor_testlib import assert_contract
from tutor import pipeline
from tutor.runtime import budget
from tutor.runtime.response import TUTOR_DEFAULT

json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(),
    lambda inner: (
        st.lists(inner, max_size=5) | st.dictionaries(st.text(max_size=10), inner, max_size=5)
    ),
    max_leaves=30,
)
message_bodies = st.fixed_dictionaries(
    {
        "messages": st.lists(
            st.fixed_dictionaries(
                {"role": st.sampled_from(["user", "assistant", "system", ""]), "content": st.text()}
            ),
            max_size=8,
        )
    }
)


def run(coro):
    return asyncio.run(coro)


def test_handle_takes_only_the_body():  # 헤더·세션으로 분기하지 않는다(TU-18)
    assert list(inspect.signature(tutor.handle).parameters) == ["body"]


def test_handle_answers_a_normal_request_in_contract():
    assert_contract(run(tutor.handle({"messages": [{"role": "user", "content": "변수가 뭐야?"}]})))


def test_handle_never_raises_when_respond_fails(monkeypatch):
    async def boom(body, **_):
        raise RuntimeError("요청 내용이 섞인 메시지")

    monkeypatch.setattr(pipeline, "respond", boom)
    assert run(tutor.handle({"messages": [{"role": "user", "content": "q"}]})) == TUTOR_DEFAULT


def test_handle_passes_a_request_deadline(monkeypatch):
    seen = []

    async def capture(body, *, deadline, state):
        seen.append((deadline - asyncio.get_running_loop().time(), state))
        return {"answer": "a", "action": " Answer ", "citations": []}

    monkeypatch.setattr(pipeline, "respond", capture)
    out = run(tutor.handle({"messages": []}))
    assert out == {"answer": "a", "action": "answer", "citations": []}
    left, state = seen[0]
    assert 0 < left <= budget.DEFAULT_BUDGET_S * state.tutor_cfg.budget_scale  # 20 × 배율
    assert state is pipeline.default_state()  # 프로세스 안 호출은 한 번 만든 기본 state


def test_serve_passes_the_given_state(monkeypatch):  # 로컬 측정이 설정을 바꾼 state로 부른다
    seen = []

    async def capture(body, *, deadline, state):
        seen.append(state)
        return {"answer": "a", "action": "answer", "citations": []}

    monkeypatch.setattr(pipeline, "respond", capture)
    custom = object()
    assert run(pipeline.serve({"messages": []}, state=custom))["action"] == "answer"
    assert seen == [custom]


def test_handle_gives_the_default_when_startup_fails(monkeypatch, tmp_path):
    # 설정이 깨져 기동(prepare)이 실패해도 예외 없이 기본값(TU-01, #107 리뷰). @cache는 예외를
    # 남기지 않으니 부를 때마다 다시 시도하고, 그때마다 기본값이다
    pipeline.default_state.cache_clear()
    monkeypatch.setenv("TUTOR_CONFIG", str(tmp_path / "missing.yaml"))
    try:
        for _ in range(2):
            body = {"messages": [{"role": "user", "content": "q"}]}
            assert run(tutor.handle(body)) == TUTOR_DEFAULT
    finally:
        pipeline.default_state.cache_clear()  # 다음 테스트는 제대로 된 설정으로 다시 만든다


def test_handle_gives_the_default_when_respond_hangs(monkeypatch):  # 하네스가 매달리지 않는다
    monkeypatch.setattr(budget, "DEFAULT_BUDGET_S", 0.05)
    monkeypatch.setattr(budget, "BACKSTOP_MARGIN_S", 0.0)

    async def hang(body, **_):
        await asyncio.sleep(5)

    monkeypatch.setattr(pipeline, "respond", hang)
    started = time.perf_counter()
    assert run(tutor.handle({"messages": [{"role": "user", "content": "q"}]})) == TUTOR_DEFAULT
    assert time.perf_counter() - started < 2


@settings(max_examples=150, deadline=None)
@given(body=json_values | message_bodies)
def test_handle_fuzz_always_in_contract(body):
    assert_contract(run(tutor.handle(body)))


def test_startup_takes_app_state():
    class State:
        pass

    assert run(tutor.startup(State())) is None
