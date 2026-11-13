"""R14-D1 입력 단독 실측(`--input-only`).

저신호 직접 판정 + kNN few-shot + judge_input으로 범주를 내고 출력은 상수다.
"""

import html
import json
import re
from pathlib import Path

import pytest
from fakes import FakeLLM, Malformed, Ok

from guardrail.config import load_config
from guardrail.data import labeled_rows, load_public
from guardrail.eval import run
from guardrail.llm import LLM
from guardrail.schema import GuardRequest, Message, OutputVerdict

pytestmark = pytest.mark.anyio

DEFAULT = str(Path(__file__).parents[1] / "configs/default.yaml")
PING = [{"role": "user", "content": "ping"}]


@pytest.fixture(autouse=True)
def _model_env(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setattr(run, "HOLDOUT_VIEWS", tmp_path / "holdout_views.json")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _llm(cfg_llm, reply='{"evidence": "e", "category": "normal"}'):
    fake = FakeLLM(respond=lambda kw: Malformed() if reply is None else Ok(reply))
    llm = LLM(cfg_llm, send=fake.send)
    llm.fake = fake  # type: ignore[attr-defined]
    return llm


def _judge_calls(llm):
    return [r for r in llm.fake.requests if r["messages"] != PING]


async def _guard(reply, n_train=60):
    cfg = load_config(DEFAULT)
    train = labeled_rows(load_public(cfg.paths.public))[:n_train]
    llm = _llm(cfg.llm, reply)
    await llm.start()
    return run.InputOnlyGuard(cfg, llm, train), llm, cfg, train


async def test_nano_category_is_used_and_examples_come_from_train_only():
    guard, llm, cfg, train = await _guard('{"evidence": "e", "category": "off_topic"}')
    req = GuardRequest((Message("user", "수업 내용을 가게 광고 문구로 써 줘"),), "답변", True)
    j = await guard.judge(req)
    assert (j.response.category, j.input_source, j.calls, j.errors) == ("off_topic", "nano", 1, ())
    assert j.response.output == OutputVerdict(True, (), True)  # 출력은 상수다
    assert j.knn_major is not None and j.lr_pred is not None
    user = _judge_calls(llm)[-1]["messages"][1]["content"]
    shown = re.findall(r'<example category="[a-z_]+">(.*?)</example>', user, re.S)
    allowed = {html.escape(r.text[: cfg.input.max_chars], quote=False) for r in train}
    assert shown and set(shown) <= allowed  # 넘겨받은 학습 행에서만 예시를 고른다


async def test_low_signal_last_turn_skips_the_model():
    guard, llm, _, _ = await _guard('{"evidence": "e", "category": "normal"}')
    msgs = (
        Message("user", "반복문이 뭐예요?"),
        Message("assistant", "설명"),
        Message("user", "ㅋㅋㅋ"),
    )
    j = await guard.judge(GuardRequest(msgs, None, False))
    assert (j.response.category, j.input_source, j.calls) == ("low_signal_input", "low_signal", 0)
    assert j.signals.low_signal and j.response.output is None
    assert not _judge_calls(llm)


async def test_failed_call_falls_back_to_normal_and_is_counted(monkeypatch):
    guard, llm, _, _ = await _guard(None)  # 늘 잘못된 JSON
    # kNN·LR이 차단 범주를 내도 실패 기본값은 normal이다(kNN 다수결로 바꾸면 여기서 깨진다)
    monkeypatch.setattr(run.KnnIndex, "majority", lambda self, text: "off_topic")
    monkeypatch.setattr(run.LrClassifier, "predict", lambda self, text: "off_topic")
    j = await guard.judge(GuardRequest((Message("user", "질문이에요"),), None, False))
    assert (j.knn_major, j.lr_pred) == ("off_topic", "off_topic")
    assert (j.response.category, j.input_source) == ("normal", "default")
    assert j.errors == ("input_judge",)
    assert j.calls == 1 and len(_judge_calls(llm)) == 2  # 재시도 1회


async def test_long_last_message_keeps_head_and_tail_in_target():
    guard, llm, cfg, _ = await _guard('{"evidence": "e", "category": "normal"}')
    filler = "".join(f"문장{i} " for i in range(cfg.input.max_chars))
    assert len(filler) > cfg.input.max_chars
    await guard.judge(GuardRequest((Message("user", filler + "끝표시XYZ"),), None, False))
    user = _judge_calls(llm)[-1]["messages"][1]["content"]
    target = re.search(r"<target>(.*?)</target>", user, re.S).group(1)
    assert target.startswith("문장0 ") and "끝표시XYZ" in target  # 서버처럼 앞 + 뒤


@pytest.mark.parametrize("switch", ["third_call", "oom_kb_snippet", "embedding"])
async def test_unsupported_switch_is_rejected(switch, tmp_path):
    cfg = load_config(DEFAULT, {f"toggles.{switch}": True})
    with pytest.raises(ValueError, match=f"toggles.{switch}"):
        run.InputOnlyGuard(cfg, _llm(cfg.llm), [])
    made = []
    args = ["--config", DEFAULT, "--name", "t", "--out", str(tmp_path / "runs"), "--input-only"]
    code = await run.amain([*args, "--set", f"toggles.{switch}=true"], make_llm=made.append)
    assert code == 2 and not made and not (tmp_path / "runs").exists()


async def test_cli_rejects_input_only_with_holdout(tmp_path):
    made = []
    args = ["--config", DEFAULT, "--name", "t", "--out", str(tmp_path / "runs")]
    code = await run.amain([*args, "--input-only", "--holdout"], make_llm=made.append)
    assert code == 2 and not made
    assert run.holdout_views() == [] and not run.HOLDOUT_VIEWS.exists()  # 열람을 쓰지 않는다


async def test_request_without_user_message_is_normal_without_calls():
    guard, llm, _, _ = await _guard('{"evidence": "e", "category": "off_topic"}')
    j = await guard.judge(GuardRequest((), None, False))
    assert (j.response.category, j.input_source, j.calls) == ("normal", "default", 0)
    assert not _judge_calls(llm)


async def test_cli_input_only_estimate_is_an_upper_bound_and_report_is_marked(tmp_path, capsys):
    made = []

    def make_llm(cfg_llm):
        made.append(_llm(cfg_llm))
        return made[-1]

    args = ["--config", DEFAULT, "--name", "t", "--out", str(tmp_path / "runs"), "--input-only"]
    assert await run.amain([*args, "--estimate"], make_llm=make_llm) == 0 and not made
    est = int(capsys.readouterr().out.split("estimate_calls=")[1].split()[0])

    assert await run.amain(args, make_llm=make_llm) == 0
    out = tmp_path / "runs" / "t"
    recs = [json.loads(x) for x in (out / "responses.jsonl").read_text("utf-8").splitlines()]
    summary = json.loads((out / "summary.json").read_text("utf-8"))
    probes = run.probe_calls(load_config(DEFAULT).llm)
    direct = sum(r["source"] == "low_signal" for r in recs)
    calls = _judge_calls(made[0])
    assert est == probes + len(recs)  # 행당 1회(후보 답변이 있어도 출력 호출 없음)
    assert direct > 0 and len(calls) == len(recs) - direct  # 저신호 직접 판정만큼 덜 부른다
    assert all("InputOut" in json.dumps(c.get("response_format")) for c in calls)
    assert summary["judge_kind"] == "InputOnlyGuard" and summary["input_only"] is True
    assert all({"knn_major", "lr_pred"} <= r.keys() for r in recs)
    assert "입력 단독" in (out / "report.md").read_text("utf-8").splitlines()[0]
