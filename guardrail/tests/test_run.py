"""S2.9 평가 하네스 골격: ConstantGuard 400행, 호출 수 예측, probe 1회, 상한, 홀드아웃, ablation."""

import json
import time
from pathlib import Path

import pytest
from fakes import FakeLLM, Ok

from guardrail.config import load_config
from guardrail.data import load_public
from guardrail.eval import cv, run
from guardrail.eval.fake_llm import make_fake_llm
from guardrail.guard import ConstantGuard
from guardrail.judges.schemas import InputOut, OutputBaselineOut

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).parents[1]
BASELINE = str(ROOT / "configs/baseline.yaml")
DEFAULT = str(ROOT / "configs/default.yaml")


@pytest.fixture(autouse=True)
def _model_env(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "test-model")


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def constant_factory(cfg, llm, train):
    return ConstantGuard()


def _args(tmp_path, *extra, config=BASELINE):
    return [
        "--config", config, "--name", "t", "--out", str(tmp_path / "runs"),
        "--holdout-log", str(tmp_path / "holdout.jsonl"), *extra,
    ]  # fmt: skip


async def test_constant_guard_runs_all_400_rows_with_holdout(tmp_path):
    code = await run.amain(_args(tmp_path, "--fake", "--holdout"), judge_factory=constant_factory)
    assert code == 0
    out = tmp_path / "runs" / "t"
    lines = (out / "responses.jsonl").read_text(encoding="utf-8").splitlines()
    ids = {json.loads(x)["id"] for x in lines}
    assert len(lines) == len(ids) == 400
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["configs"]["t"]["scores"][0]["cv"]["guard_score"] == pytest.approx(
        29.48, abs=8
    )  # 상수 응답: 전부 allow 29.48은 가드 200행 값, CV 부분집합은 근처
    assert (out / "report.md").read_text(encoding="utf-8").startswith("# ")


async def test_without_holdout_flag_only_cv_rows_are_judged(tmp_path):
    await run.amain(_args(tmp_path, "--fake"), judge_factory=constant_factory)
    lines = (tmp_path / "runs" / "t" / "responses.jsonl").read_text(encoding="utf-8").splitlines()
    held = set(cv.load_splits().holdout)
    split = cv.load_splits()
    assert 0 < len(lines) < 400
    assert all(split.cluster[json.loads(x)["id"]] not in held for x in lines)
    assert not (tmp_path / "holdout.jsonl").exists()


def _counting_llm(cfg_llm):
    def respond(kw):
        text = json.dumps(kw["messages"], ensure_ascii=False)
        if "OutputBaselineOut" in json.dumps(kw.get("response_format", {})):
            return Ok('{"reason": "r", "grounded": true, "safe": true}')
        assert text
        return Ok('{"evidence": "e", "category": "normal"}')

    fake = FakeLLM(respond=respond)
    from guardrail.llm import LLM

    llm = LLM(cfg_llm, send=fake.send)
    llm.fake = fake  # type: ignore[attr-defined]
    return llm


async def _calling_factory(cfg, llm, train):
    """판정기 대역: 행마다 입력 1회, 후보 답변이 있으면 출력 1회(baseline 설정 예측과 같다)."""

    class J:
        async def judge(self, req):
            text = req.messages[-1].content if req.messages else ""
            dl = time.monotonic() + 30
            await llm.complete(
                "input_judge", [{"role": "user", "content": text}], InputOut, deadline=dl
            )
            if req.has_candidate:
                await llm.complete(
                    "output_judge",
                    [{"role": "user", "content": text}],
                    OutputBaselineOut,
                    deadline=dl,
                )
            return await ConstantGuard().judge(req)

    return J()


async def _double_calling_factory(cfg, llm, train):
    inner = await _calling_factory(cfg, llm, train)

    class J:
        async def judge(self, req):
            await inner.judge(req)
            return await inner.judge(req)  # 예측의 두 배를 부른다

    return J()


async def test_estimate_equals_actual_and_probe_runs_once_for_five_folds(tmp_path, capsys):
    made = []

    def make_llm(cfg_llm):
        llm = _counting_llm(cfg_llm)
        made.append(llm)
        return llm

    code = await run.amain(_args(tmp_path, "--estimate"), make_llm=make_llm)
    assert code == 0 and not made  # --estimate는 LLM을 만들지 않는다(호출 0)
    est = int(capsys.readouterr().out.split("estimate_calls=")[1].split()[0])

    code = await run.amain(_args(tmp_path), make_llm=make_llm, judge_factory=_calling_factory)
    assert code == 0 and len(made) == 1
    reqs = made[0].fake.requests
    probes = [r for r in reqs if r["messages"] == [{"role": "user", "content": "ping"}]]
    assert len(probes) == run.probe_calls(load_config(BASELINE).llm)  # 폴드 5개여도 probe는 한 번
    assert len(reqs) == est


async def test_over_max_calls_stops_before_start(tmp_path):
    made = []

    def make_llm(cfg_llm):
        made.append(_counting_llm(cfg_llm))
        return made[-1]

    code = await run.amain(
        _args(tmp_path, "--max-calls", "10"), make_llm=make_llm, judge_factory=_calling_factory
    )
    assert code == 3
    assert not made or not made[0].fake.requests  # probe조차 보내지 않았다
    assert not (tmp_path / "runs" / "t").exists()


async def test_holdout_is_refused_on_fourth_open(tmp_path):
    for _ in range(3):
        code = await run.amain(
            _args(tmp_path, "--fake", "--holdout"), judge_factory=constant_factory
        )
        assert code == 0
    log = (tmp_path / "holdout.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(log) == 3 and json.loads(log[0])["name"] == "t"
    code = await run.amain(_args(tmp_path, "--fake", "--holdout"), judge_factory=constant_factory)
    assert code == 4
    assert len((tmp_path / "holdout.jsonl").read_text(encoding="utf-8").splitlines()) == 3


async def test_estimate_does_not_log_holdout_open(tmp_path):
    await run.amain(_args(tmp_path, "--estimate", "--holdout"))
    assert not (tmp_path / "holdout.jsonl").exists()


def test_ablation_files_each_toggle_one_switch():
    base = load_config(DEFAULT).toggles.model_dump()
    names = run.ablation_files(ROOT / "configs/ablation")
    assert len(names) == 11
    for name, path in names.items():
        t = load_config(DEFAULT, ablation=path).toggles.model_dump()
        diff = {k for k in t if t[k] != base[k]}
        assert len(diff) == 1, name
        assert name == ("no_" if base[next(iter(diff))] else "with_") + next(iter(diff))


async def test_ablation_only_selects_rows_and_keeps_baseline_default(tmp_path, capsys):
    argv = ["--ablation", str(ROOT / "configs/ablation"), "--only", "no_lr,with_embedding",
            "--config", DEFAULT, "--name", "a", "--estimate", "--repeat", "2"]  # fmt: skip
    assert await run.amain(argv) == 0
    out = capsys.readouterr().out
    assert "rows=baseline,default,no_lr,with_embedding" in out
    assert await run.amain([*argv[:3], "nope", *argv[4:]]) == 2


async def test_fake_llm_answers_both_schemas():
    llm = make_fake_llm(load_config(DEFAULT).llm)
    await llm.start()
    dl = time.monotonic() + 5
    a = await llm.complete("input_judge", [{"role": "user", "content": "x"}], InputOut, deadline=dl)
    b = await llm.complete(
        "output_judge", [{"role": "user", "content": "x"}], OutputBaselineOut, deadline=dl
    )
    assert a.parsed.category == "normal" and b.parsed.grounded is True


def test_set_override_parses_yaml_values():
    assert run.parse_sets(["toggles.lr=false", "budget_s=7"]) == {
        "toggles.lr": False,
        "budget_s": 7,
    }
    with pytest.raises(ValueError):
        run.parse_sets(["nokey"])


async def test_rejects_holdout_with_ablation_and_unsafe_name(tmp_path):
    abl = str(ROOT / "configs/ablation")
    assert await run.amain(_args(tmp_path, "--fake", "--holdout", "--ablation", abl)) == 2
    bad = ["--config", BASELINE, "--name", "a/b", "--fake"]
    assert await run.amain(bad) == 2
    assert not (tmp_path / "holdout.jsonl").exists()


async def test_budget_errors_make_the_run_invalid(tmp_path):
    # 예측은 통과하지만 실제로는 상한에 걸리게: 상한을 예측 바로 아래 호출 수로 둔다
    est = run.estimate_calls(
        {"t": load_config(BASELINE)},
        [r for r in load_public(load_config(BASELINE).paths.public)],
        1,
        load_config(BASELINE).llm,
    )
    code = await run.amain(
        _args(tmp_path, "--max-calls", str(est)), make_llm=_counting_llm,
        judge_factory=_double_calling_factory,
    )  # fmt: skip
    assert code == 5
