"""요청 해석(TU-01·TU-02): 예외 없이 읽을 수 있는 만큼 읽는다."""

import pytest

from tutor.request import Turn, TutorRequest, load_json, parse_request


def test_reads_messages_and_last_user():
    req = parse_request(
        {
            "spec_version": "1.0",
            "session_id": "s",
            "messages": [{"role": "user", "content": "안녕"}],
        }
    )
    assert req == TutorRequest((Turn("user", "안녕"),))
    assert req.last_user == "안녕"


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        "x",
        1,
        {},
        {"messages": None},
        {"messages": "x"},
        {"messages": {}},
        {"messages": []},
        {"messages": [1, "a", None, {"role": "user"}]},
    ],
)
def test_unreadable_is_none(body):
    assert parse_request(body) is None


def test_keeps_string_content_and_blanks_bad_roles():
    req = parse_request(
        {
            "messages": [
                {"role": 1, "content": "a"},
                {"role": "user", "content": 2},
                {"content": "b"},
            ]
        }
    )
    assert req.turns == (Turn("", "a"), Turn("", "b"))
    assert req.last_user is None


def test_last_user_when_order_is_broken():
    req = parse_request(
        {
            "messages": [
                {"role": "assistant", "content": "x"},
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "y"},
            ]
        }
    )
    assert req.last_user == "q1"


def test_spec_version_and_session_do_not_matter():  # 동작은 messages로만 정한다(TU-18)
    msgs = [{"role": "user", "content": "q"}]
    a = parse_request({"spec_version": "1.0", "session_id": "a", "messages": msgs})
    b = parse_request({"spec_version": "9.9", "session_id": "b", "messages": msgs})
    assert a == b


def test_load_json_reads_objects():
    assert load_json(b'{"a": [1, "\xea\xb0\x80"]}') == {"a": [1, "가"]}


@pytest.mark.parametrize(
    "raw",
    [b"", b"not json", b"\xff\xfe\xfa", b"[" * 100_000 + b"]" * 100_000, b"1" * 5000],
)
def test_load_json_never_raises(raw):
    assert load_json(raw) is None


def _req(*pairs):
    return parse_request({"messages": [{"role": r, "content": c} for r, c in pairs]})


def test_window_keeps_the_first_learner_turn_and_the_last_six():
    pairs = [("user", "q1"), ("assistant", "a1"), ("user", "q2"), ("assistant", "a2")]
    pairs += [
        ("user", "q3"),
        ("assistant", "a3"),
        ("user", "q4"),
        ("assistant", "a4"),
        ("user", "q5"),
    ]
    req = _req(*pairs)
    # 창 = 첫 학습자 발화 + 마지막 메시지 6개(TU-02, 가드 GR-04와 같음) → a1·q2는 창 밖
    assert [t.content for t in req.window()] == ["q1", "a2", "q3", "a3", "q4", "a4", "q5"]
    assert req.search_query() == ("q5", ["q1", "q3", "q4"])  # 검색 질의(recall 측정과 같은 규칙)


def test_search_query_without_a_learner_turn_is_empty():
    assert _req(("assistant", "a")).search_query() == ("", [])
    assert _req(("user", "only")).search_query() == ("only", [])


def test_window_always_keeps_the_last_learner_turn():  # #108 리뷰(규격 밖 입력)
    pairs = [("user", "q1"), ("user", "q2")] + [("assistant", f"a{i}") for i in range(6)]
    req = _req(*pairs)
    # 뒤에 튜터 발화가 6개 붙어도 판단 대상(q2)은 창 안에 있다(#97 tidy와 같은 규칙)
    assert req.search_query() == ("q2", ["q1"]) and req.last_user == "q2"
    assert [t.content for t in req.window()] == ["q1", "q2", "a0", "a1", "a2", "a3", "a4", "a5"]


def test_window_keeps_the_first_question_when_the_last_one_is_far_back():
    pairs = [("user", "q")] + [("assistant", f"a{i}") for i in range(7)]
    req = _req(*pairs)
    # 마지막 학습자 발화가 마지막 6개 밖이면 첫 학습자 발화로 찾는다(가드 GR-04와 같은 창)
    assert req.search_query() == ("q", []) and req.last_user == "q"
    assert [t.content for t in req.window()][-1] == "a6"
