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
