"""출력 단독 실측(`--output-only`): KB 검색 → 문맥 → ask_grounded. 입력은 상수(normal)다."""

import json
from pathlib import Path

import pytest
from fakes import FakeLLM, Malformed, Ok

from guardrail.config import load_config
from guardrail.data import load_public
from guardrail.eval import run
from guardrail.llm import LLM
from guardrail.schema import GuardRequest, Message, OutputVerdict

pytestmark = pytest.mark.anyio

DEFAULT = str(Path(__file__).parents[1] / "configs/default.yaml")
PING = [{"role": "user", "content": "ping"}]
UNGROUNDED = '{"answer_quote": "석 달", "kb_quote": "", "grounded": false, "refuses": false}'
MSGS = (Message("user", "리스트에 값을 더하려면 어떻게 해요?"),)


@pytest.fixture(autouse=True)
def _model_env(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setattr(run, "HOLDOUT_VIEWS", tmp_path / "holdout_views.json")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _llm(cfg_llm, reply=UNGROUNDED):
    fake = FakeLLM(respond=lambda kw: Malformed() if reply is None else Ok(reply))
    llm = LLM(cfg_llm, send=fake.send)
    llm.fake = fake  # type: ignore[attr-defined]
    return llm


def _judge_calls(llm):
    return [r for r in llm.fake.requests if r["messages"] != PING]


async def _guard(reply=UNGROUNDED, failure_default="grounded", mode=None):
    cfg = load_config(DEFAULT)
    update = {"failure_default": failure_default} | ({"mode": mode} if mode else {})
    out_cfg = cfg.output.model_copy(update=update)
    cfg = cfg.model_copy(update={"output": out_cfg})
    llm = _llm(cfg.llm, reply)
    await llm.start()
    return run.OutputOnlyGuard(cfg, llm), llm


async def test_nano_verdict_is_recorded_and_sent_with_kb_context():
    guard, llm = await _guard()
    j = await guard.judge(GuardRequest(MSGS, "append를 쓰면 리스트 끝에 값이 붙어요.", True))
    assert j.response.category == "normal" and j.input_source == "default"  # 입력은 상수다
    assert j.response.output == OutputVerdict(False, (), True)  # nano 값 그대로, 구간은 내지 않는다
    assert (j.nano_grounded, j.calls, j.errors) == (False, 1, ())
    user = _judge_calls(llm)[-1]["messages"][1]["content"]
    assert "<question>" in user and "<answer>append를" in user and '<kb id="' in user
    assert "OutputOut" in str(_judge_calls(llm)[-1]["response_format"])


async def test_kb_search_ignores_hidden_chars_interleaved_in_the_answer():
    answer = "append를 쓰면 리스트 끝에 값이 붙어요."
    kbs = []
    for a in (answer, "​".join(answer)):
        guard, llm = await _guard()
        await guard.judge(GuardRequest((Message("user", "안녕"),), a, True))
        user = _judge_calls(llm)[-1]["messages"][1]["content"]
        kbs.append(user[user.index("</answer>") :])
    assert '<kb id="' in kbs[0] and kbs[0] == kbs[1]


def _section(llm, tag):
    user = _judge_calls(llm)[-1]["messages"][1]["content"]
    return user[user.index(f"<{tag}>") : user.index(f"</{tag}>")]


async def test_question_is_the_last_user_message():
    guard, llm = await _guard()
    msgs = (Message("user", "첫질문AAA"), Message("assistant", "답"), Message("user", "끝질문BBB"))
    await guard.judge(GuardRequest(msgs, "append를 쓰면 리스트 끝에 값이 붙어요.", True))
    question = _section(llm, "question")
    assert "끝질문BBB" in question and "첫질문AAA" not in question


async def test_hidden_char_in_the_answer_reaches_nano_as_a_mark():
    guard, llm = await _guard()
    await guard.judge(GuardRequest(MSGS, "append를​ 쓰면 리스트 끝에 값이 붙어요.", True))
    answer = _section(llm, "answer")
    assert "&lt;U+200B&gt;" in answer and "​" not in answer


async def test_answer_past_the_cap_is_not_sent():
    guard, llm = await _guard()
    cap = guard.cfg.input.max_answer_chars
    await guard.judge(GuardRequest(MSGS, "가" * cap + "꼬리표시ZZZ", True))
    assert "꼬리표시ZZZ" not in _section(llm, "answer")


async def test_baseline_mode_asks_with_the_baseline_schema_and_prompt():
    reply = '{"reason": "r", "grounded": false, "safe": true}'
    default_guard, default_llm = await _guard()
    await default_guard.judge(GuardRequest(MSGS, "append를 쓰면 리스트 끝에 값이 붙어요.", True))
    guard, llm = await _guard(reply, mode="baseline")
    j = await guard.judge(GuardRequest(MSGS, "append를 쓰면 리스트 끝에 값이 붙어요.", True))
    call = _judge_calls(llm)[-1]
    assert "OutputBaselineOut" in str(call["response_format"])
    assert call["messages"][0] != _judge_calls(default_llm)[-1]["messages"][0]
    assert (j.response.output.grounded, j.errors) == (False, ())


async def test_no_candidate_or_empty_candidate_makes_no_call():
    guard, llm = await _guard()
    none = await guard.judge(GuardRequest(MSGS, None, False))
    empty = await guard.judge(GuardRequest(MSGS, "", True))
    assert none.response.output is None and none.calls == 0
    assert empty.response.output == OutputVerdict(True, (), True) and empty.calls == 0
    assert not _judge_calls(llm)


@pytest.mark.parametrize(("setting", "want"), [("grounded", True), ("ungrounded", False)])
async def test_failed_call_follows_failure_default_and_is_counted(setting, want):
    guard, llm = await _guard(None, setting)
    j = await guard.judge(GuardRequest(MSGS, "답변이에요", True))
    assert j.response.output == OutputVerdict(want, (), True)
    assert (j.nano_grounded, j.errors, j.calls) == (None, ("output_judge",), 1)
    assert len(_judge_calls(llm)) == 2  # 재시도 1회


async def test_cli_output_only_calls_only_rows_with_a_candidate(tmp_path, capsys):
    made = []

    def make_llm(cfg_llm):
        made.append(_llm(cfg_llm))
        return made[-1]

    args = ["--config", DEFAULT, "--name", "t", "--out", str(tmp_path / "runs"), "--output-only"]
    assert await run.amain([*args, "--estimate"], make_llm=make_llm) == 0 and not made
    est = int(capsys.readouterr().out.split("estimate_calls=")[1].split()[0])

    assert await run.amain(args, make_llm=make_llm) == 0
    out = tmp_path / "runs" / "t"
    recs = [json.loads(x) for x in (out / "responses.jsonl").read_text("utf-8").splitlines()]
    summary = json.loads((out / "summary.json").read_text("utf-8"))
    public = {r.id: r for r in load_public(load_config(DEFAULT).paths.public)}
    with_answer = [r for r in recs if public[r["id"]].candidate_answer]
    assert 0 < len(with_answer) < len(recs)
    assert est == run.probe_calls(load_config(DEFAULT).llm) + len(with_answer)
    assert len(_judge_calls(made[0])) == len(with_answer)
    assert all(r["llm"][0]["parsed"]["answer_quote"] == "석 달" for r in with_answer)
    assert all(r["llm"] == [] for r in recs if not public[r["id"]].candidate_answer)
    assert summary["judge_kind"] == "OutputOnlyGuard" and summary["output_only"] is True
    assert "출력 단독" in (out / "report.md").read_text("utf-8").splitlines()[0]


async def test_input_only_and_output_only_together_is_a_usage_error(tmp_path):
    args = ["--config", DEFAULT, "--name", "t", "--out", str(tmp_path / "runs")]
    assert await run.amain([*args, "--input-only", "--output-only", "--fake"]) == run.EXIT_USAGE
