"""handle 계약(TU-01): 어떤 입력에도 예외 없이 튜터 규격 응답. 요청 사이 상태 없음(TU-18)."""

import asyncio
import inspect

from hypothesis import given, settings
from hypothesis import strategies as st

import tutor
from tests.tutor.tutor_testlib import assert_contract
from tutor import pipeline
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
    async def boom(body):
        raise RuntimeError("요청 내용이 섞인 메시지")

    monkeypatch.setattr(pipeline, "respond", boom)
    assert run(tutor.handle({"messages": [{"role": "user", "content": "q"}]})) == TUTOR_DEFAULT


@settings(max_examples=150, deadline=None)
@given(body=json_values | message_bodies)
def test_handle_fuzz_always_in_contract(body):
    assert_contract(run(tutor.handle(body)))


def test_startup_takes_app_state():
    class State:
        pass

    assert run(tutor.startup(State())) is None
