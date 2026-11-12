"""통합 E2E 퍼징의 hypothesis 전략(I1.7). 가드 `guardrail/tests/strategies.py`와 같은 모양이다.

요청 본문은 바이트로 만든다. 서버가 받는 것이 바이트이기 때문이다.
"""

import json
from typing import Any

import hypothesis.strategies as st

# 짝 없는 surrogate · 제어 문자 · 보이지 않는 문자까지 전부 나오는 글자
any_char = st.characters(exclude_categories=())
odd_text = st.text(alphabet=any_char, max_size=40)
# 한 글자를 길게 늘인 문자열(예시 데이터는 작게 두고 본문만 키운다). 3,000자는 규격 예시 길이,
# 20,000자는 답변 상한(4,000자)을 크게 넘는 길이다.
long_text = st.builds(
    lambda unit, count: unit * count,
    st.sampled_from(["가", "a ", "​", "\ud800", "\x00", "\n", "😀", "<학습자 발화>"]),
    st.sampled_from([3_000, 20_000]),
)

json_scalars = (
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=20)
)
# 임의 JSON 값: 중첩 dict · list · 문자열 · 숫자 · null
json_values = st.recursive(
    json_scalars,
    lambda inner: (
        st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=8), inner, max_size=4)
    ),
    max_leaves=20,
)

_roles = (
    st.sampled_from(["user", "assistant", "system", "tool", "", "USER", " user "])
    | st.integers()
    | st.none()
    | st.lists(st.text(max_size=4), max_size=2)
    | odd_text
)
_contents = (
    odd_text
    | long_text
    | st.integers()
    | st.none()
    | st.booleans()
    | st.lists(odd_text, max_size=3)
    | st.dictionaries(st.text(max_size=4), odd_text, max_size=2)
)
_message = st.fixed_dictionaries({}, optional={"role": _roles, "content": _contents})
# messages 모양(list of dict)은 맞지만 값이 이상한 본문. 가끔 dict가 아닌 항목도 섞는다
odd_bodies = st.fixed_dictionaries(
    {"messages": st.lists(_message | json_scalars, max_size=6)},
    optional={
        "spec_version": st.sampled_from(["1.0", "9.9", ""]) | st.integers() | st.none(),
        "session_id": json_scalars | odd_text,
        "candidate_answer": odd_text | long_text | st.integers() | st.none() | st.lists(odd_text),
    },
)


def to_bytes(value: Any) -> bytes:
    """JSON 바이트. 짝 없는 surrogate는 `\\udXXX` 이스케이프로 나간다(UTF-8로는 실을 수 없다)."""
    return json.dumps(value, ensure_ascii=True).encode("ascii")


random_bytes = st.binary(max_size=2_048)
json_bodies = json_values.map(to_bytes)
odd_message_bodies = odd_bodies.map(to_bytes)
