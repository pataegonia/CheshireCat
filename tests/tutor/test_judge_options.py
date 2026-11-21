"""판정 설정 바꾸기(--judge-effort·--judge-max-tokens·--judge-repeat)와 응답 저장·재채점. API 0회.

운영진 판정기(competition/grader/judge.py)는 고치지 않는다. 기본값이면 그 판정기를 그대로 쓰고,
설정을 주면 같은 프롬프트·요청 모양에 reasoning_effort·상한만 바꾼 `grader.tuned_judge`를 쓴다.
"""

import io
import json
from collections import Counter

import pytest

from tests.tutor import tutor_testlib
from tutor.eval import grader, run_local

ENV = {"OPENAI_BASE_URL": "http://judge.invalid/v1", "OPENAI_MODEL": "m", "OPENAI_API_KEY": "k"}


class FakeResp(io.BytesIO):
    headers = {"Content-Type": "text/event-stream"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def stream(content: str, finish: str = "stop") -> FakeResp:
    chunks = [
        {"choices": [{"delta": {"content": content[:3]}}]},
        {"choices": [{"delta": {"content": content[3:]}, "finish_reason": finish}]},
    ]
    lines = [f"data: {json.dumps(c)}\n" for c in chunks] + ["data: [DONE]\n"]
    return FakeResp("".join(lines).encode())


@pytest.fixture
def sent(monkeypatch):
    """urlopen을 가짜로: replies 차례로 돌려주고 보낸 본문을 남긴다."""
    box = {"bodies": [], "replies": []}

    def urlopen(req, timeout):
        box["bodies"].append(json.loads(req.data))
        reply = box["replies"].pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(grader.urllib.request, "urlopen", urlopen)
    return box


def test_default_judge_is_the_grader_judge(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    judge = grader.judge_from_env()
    assert judge is not None and not hasattr(judge, "stats")  # 운영진 make_judge 그대로
    assert hasattr(grader.judge_from_env("medium"), "stats")


def test_tuned_judge_sends_effort_and_max_tokens_with_the_grader_prompt(sent):
    judge = grader.tuned_judge(
        "http://judge.invalid/v1", "m", "k", effort="medium", max_tokens=9000
    )
    sent["replies"] = [stream('{"ok": true}')]
    assert judge("faithful", {"answer": "a", "evidence": []}) == {"ok": True}
    body = sent["bodies"][0]
    assert body["reasoning_effort"] == "medium" and body["max_completion_tokens"] == 9000
    assert body["stream"] is True and body["response_format"] == {"type": "json_object"}
    system = body["messages"][0]["content"]
    assert system == f"{grader._judge.SYSTEM}\n{grader._judge.PROMPTS['faithful']}"
    no_effort = grader.tuned_judge("http://judge.invalid/v1", "m", "k", effort=None, max_tokens=1)
    sent["replies"] = [stream('{"ok": true}')]
    no_effort("faithful", {})
    assert "reasoning_effort" not in sent["bodies"][1]


def test_truncation_is_counted_and_retried_like_the_grader(sent):
    judge = grader.tuned_judge("http://judge.invalid/v1", "m", "k", effort="high", max_tokens=10)
    sent["replies"] = [stream("", "length"), stream('{"score": 0.5}')]
    assert judge("facts", {}) == {"score": 0.5}
    assert judge.stats == Counter({"attempts": 2, "length_facts": 1})
    sent["replies"] = [OSError("x")] * 3
    with pytest.raises(grader.scoring.JudgeError):
        judge("facts", {})


def test_repeat_takes_the_majority_and_the_median(sent):
    judge = grader.tuned_judge(
        "http://judge.invalid/v1", "m", "k", effort=None, max_tokens=5, repeat=3
    )
    sent["replies"] = [stream(f'{{"complied": {v}}}') for v in ("true", "false", "false")]
    assert judge("complied", {}) == {"complied": False}
    sent["replies"] = [stream(f'{{"score": {v}}}') for v in (0, 1, 0.5)]
    assert judge("facts", {}) == {"score": 0.5}
    # 동률(한 번 실패)이면 첫 판정
    sent["replies"] = [stream('{"ok": true}'), stream('{"ok": false}')] + [OSError("x")] * 3
    assert judge("faithful", {}) == {"ok": True}


def fake_judge(seen):
    def judge(kind, payload):
        seen.append(kind)
        return {
            "facts": {"score": 0.5},
            "faithful": {"ok": False},
            "complied": {"complied": False},
        }[kind]

    judge.stats = Counter({"attempts": 7, "length_facts": 2})
    return judge


def test_responses_rescored_by_a_tuned_judge_keep_item_scores_without_tutor_calls(
    tmp_path, monkeypatch
):
    seen: list[str] = []
    got = {}

    def from_env(effort=None, max_tokens=None, repeat=1):
        got.update(effort=effort, max_tokens=max_tokens, repeat=repeat)
        return fake_judge(seen)

    monkeypatch.setattr(run_local, "judge_from_env", from_env)
    sent = tutor_testlib.answer_openai(monkeypatch)  # 튜터 전송이 불리면 여기에 남는다
    saved = tmp_path / "base.jsonl"
    argv = ["--preset", "tutor", "--out", str(tmp_path), "--runs", "2"]
    assert run_local.main([*argv, "--save-responses", str(saved)]) == 0
    files = sorted(tmp_path.glob("base-r*.jsonl"))
    assert [f.name for f in files] == ["base-r0.jsonl", "base-r1.jsonl"]
    first = run_local.load_responses(files[0])
    assert first and all(isinstance(r, dict) and "answer" in r for r in first.values())
    before = len(sent)
    rows = tmp_path / "rows.jsonl"
    argv = ["--responses", *map(str, files), "--out", str(tmp_path), "--rows-jsonl", str(rows)]
    argv += ["--judge", "--judge-effort", "medium", "--judge-max-tokens", "9000"]
    assert run_local.main(argv) == 0
    assert len(sent) == before  # 재채점은 튜터를 부르지 않는다
    assert got == {"effort": "medium", "max_tokens": 9000, "repeat": 1}
    recs = [json.loads(x) for x in rows.read_text(encoding="utf-8").splitlines()]
    assert {r["run"] for r in recs} == {0, 1}
    judged = [r for r in recs if r["facts"] is not None]
    assert judged and all(r["facts"] == 0.5 for r in judged)
    assert any(r["faithful"] == 0.0 for r in recs)
    data = json.loads(next(tmp_path.glob("base-r0-*.json")).read_text(encoding="utf-8"))
    assert data["judge_config"]["effort"] == "medium" and data["judge_config"]["truncated"] == 2
    assert data["report"]["rows"] == len(first)


def test_judge_options_need_judge_and_matching_files(tmp_path, monkeypatch):
    path = tmp_path / "a.jsonl"
    path.write_text(json.dumps({"id": "x", "response": None}) + "\n", encoding="utf-8")
    other = tmp_path / "b.jsonl"
    other.write_text(json.dumps({"id": "y", "response": None}) + "\n", encoding="utf-8")
    for argv in (
        ["--responses", str(path), "--judge-effort", "medium"],
        ["--responses", str(path), "--judge-repeat", "3"],
        ["--responses", str(path), "--save-responses", str(tmp_path / "s.jsonl")],
        ["--responses", str(path), str(other)],
        ["--responses", str(path), str(path), "--runs", "2"],
    ):
        with pytest.raises(SystemExit):
            run_local.main([*argv, "--out", str(tmp_path)])


def test_expected_calls_multiply_the_judge_share_by_repeat():
    rows = run_local.public_tutor_rows()[:20]
    one = run_local.expected_calls(rows, preset=None, runs=1, judge=True)
    three = run_local.expected_calls(rows, preset=None, runs=1, judge=True, judge_repeat=3)
    assert one > 0 and abs(three - 3 * one) <= 2  # 1.1배 올림 차이만
