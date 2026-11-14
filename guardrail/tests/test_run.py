"""S2.9 평가 하네스 골격: ConstantGuard 400행, 호출 수 예측, probe 1회, 상한, 홀드아웃, ablation."""

import json
import time
from pathlib import Path
from types import SimpleNamespace

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
def _model_env(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setattr(run, "HOLDOUT_VIEWS", tmp_path / "holdout_views.json")


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def constant_factory(cfg, llm, train):
    return ConstantGuard()


def _args(tmp_path, *extra, config=BASELINE):
    return [
        "--config", config, "--name", "t", "--out", str(tmp_path / "runs"),
        *extra,
    ]  # fmt: skip


async def test_constant_guard_runs_all_400_rows_with_holdout(tmp_path):
    code = await run.amain(
        _args(tmp_path, "--holdout"), make_llm=_counting_llm, judge_factory=constant_factory
    )
    assert code == 0
    out = tmp_path / "runs" / "t"
    lines = (out / "responses.jsonl").read_text(encoding="utf-8").splitlines()
    ids = {json.loads(x)["id"] for x in lines}
    assert len(lines) == len(ids) == 400
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["configs"]["t"]["scores"][0]["cv"]["guard_score"] == pytest.approx(
        29.48, abs=8
    )  # 상수 응답: 전부 allow 29.48은 가드 200행 값, CV 부분집합은 근처
    report = (out / "report.md").read_text(encoding="utf-8")
    assert report.startswith("# FAKE/상수 판정기 — 점수 무효")
    assert summary["judge_kind"] == "ConstantGuard" and summary["fake"] is False
    assert (
        "분산 미측정" in report and "n/a" not in report.split("## baseline")[0].split("재현율")[0]
    )


async def test_without_holdout_flag_only_cv_rows_are_judged(tmp_path):
    await run.amain(_args(tmp_path, "--fake"), judge_factory=constant_factory)
    lines = (tmp_path / "runs" / "t" / "responses.jsonl").read_text(encoding="utf-8").splitlines()
    held = set(cv.load_splits().holdout)
    split = cv.load_splits()
    assert 0 < len(lines) < 400
    assert all(split.cluster[json.loads(x)["id"]] not in held for x in lines)
    assert not run.HOLDOUT_VIEWS.exists()


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
            _args(tmp_path, "--holdout"), make_llm=_counting_llm, judge_factory=constant_factory
        )
        assert code == 0
    views = run.holdout_views()
    assert len(views) == 3 and views[0]["name"] == "t" and views[0]["commit"]
    made = []

    def make_llm(cfg_llm):
        made.append(1)
        return _counting_llm(cfg_llm)

    code = await run.amain(
        _args(tmp_path, "--holdout"), make_llm=make_llm, judge_factory=constant_factory
    )
    assert code == 4 and len(run.holdout_views()) == 3
    assert not made  # start()(probe) 전에 거부한다: LLM도 만들지 않는다


async def test_estimate_does_not_log_holdout_open(tmp_path):
    await run.amain(_args(tmp_path, "--estimate", "--holdout"))
    assert not run.HOLDOUT_VIEWS.exists()


def test_ablation_files_each_toggle_one_switch():
    base = load_config(DEFAULT).toggles.model_dump()
    names = run.ablation_files(ROOT / "configs/ablation")
    assert len(names) == 11 and "no_nano_span" in names  # S2.9: nano 구간은 기본 켬
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
    assert "configs=baseline,default,no_lr,with_embedding" in out
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
    assert await run.amain(_args(tmp_path, "--holdout", "--ablation", abl)) == 2
    assert await run.amain(_args(tmp_path, "--fake", "--holdout")) == 2  # 열람 횟수를 쓰지 않는다
    bad = ["--config", BASELINE, "--name", "a/b", "--fake"]
    assert await run.amain(bad) == 2
    assert not run.HOLDOUT_VIEWS.exists()


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


def test_variance_flags_overlap_with_default():
    summary = {
        "repeat": 3,
        "configs": {
            "default": {"scores": [{"cv": {"guard_score": g}} for g in (80, 82)]},
            "near": {"scores": [{"cv": {"guard_score": g}} for g in (81, 83)]},
            "far": {"scores": [{"cv": {"guard_score": g}} for g in (90, 91)]},
        },
    }
    run.add_variance(summary)
    assert summary["variance"] == {
        "measured": True,
        "overlaps_default": {"near": True, "far": False},
    }


def test_empty_denominators_are_none_and_na():
    assert run._prf({}) == (None, None)
    assert run._range([None, None]) == "n/a"


async def test_fake_run_is_marked_invalid_in_summary_and_report(tmp_path):
    await run.amain(_args(tmp_path, "--fake"))
    out = tmp_path / "runs" / "t"
    assert json.loads((out / "summary.json").read_text(encoding="utf-8"))["fake"] is True
    assert (out / "report.md").read_text(encoding="utf-8").startswith("# FAKE/상수")


async def test_budget_invalid_banner_is_in_report(tmp_path):
    est = run.estimate_calls(
        {"t": load_config(BASELINE)},
        load_public(load_config(BASELINE).paths.public),
        1,
        load_config(BASELINE).llm,
    )
    await run.amain(
        _args(tmp_path, "--max-calls", str(est)),
        make_llm=_counting_llm,
        judge_factory=_double_calling_factory,
    )
    report = (tmp_path / "runs" / "t" / "report.md").read_text(encoding="utf-8")
    assert "# 무효 실행" in report.split("# 평가 보고서")[0]


# --- S2.8: 기본 판정기는 Guard ---


async def test_fake_run_drives_the_real_guard_end_to_end(tmp_path, capsys):
    assert await run.amain(_args(tmp_path, "--fake", config=DEFAULT)) == 0
    est = int(capsys.readouterr().out.split("estimate_calls=")[1].split()[0])
    out = tmp_path / "runs" / "t"
    summary = json.loads((out / "summary.json").read_text("utf-8"))
    recs = [json.loads(x) for x in (out / "responses.jsonl").read_text("utf-8").splitlines()]
    score = summary["configs"]["t"]["scores"][0]
    assert summary["judge_kind"] == "Guard"
    assert score["cv"]["invalid"] == 0 and score["errors"] == {}
    # 예상 호출 수는 상한이다. 저신호 직접 판정 행만큼만 덜 부른다
    low = sum(r["source"] == "low_signal" for r in recs)
    cfg = load_config(DEFAULT)
    assert low > 0 and score["calls"]["total"] + run.probe_calls(cfg.llm) == est - low
    public = {r.id: r for r in load_public(cfg.paths.public)}
    for r in recs:
        has = bool(public[r["id"]].candidate_answer)
        assert (r["tier"] is not None) == has
        # 인용 확인과 거절 판정 원값이 기록에 남는다(S3.1이 저장된 응답으로 정밀도를 잰다)
        assert all(
            (r[k] is not None) == has for k in ("answer_quote_ok", "kb_quote_ok", "nano_refuses")
        )


async def test_unsupported_ablation_row_is_refused_before_any_call(tmp_path, capsys):
    made = []

    def make_llm(cfg_llm):
        made.append(cfg_llm)
        raise AssertionError("LLM을 만들면 안 된다")

    argv = ["--ablation", str(ROOT / "configs/ablation"), "--only", "no_lr,with_third_call",
            "--name", "t", "--out", str(tmp_path / "runs")]  # fmt: skip
    assert await run.amain(argv, make_llm=make_llm) == run.EXIT_USAGE
    assert not made and "third_call" in capsys.readouterr().err
    assert not (tmp_path / "runs" / "t").exists()


# --- 속도 제한이 이어지면 멈춘다(2026-11-13: 429 뒤에도 3,700회를 더 보냈다) ---


async def test_consecutive_rate_limits_stop_the_run(tmp_path):
    from fakes import RateLimited

    from guardrail.llm import LLM

    probes = run.probe_calls(load_config(DEFAULT).llm)
    made = []

    async def nosleep(_s):
        return None

    def make_llm(cfg_llm):
        def respond(kw):
            if len(fake.requests) <= probes:  # probe는 통과, 판정 호출은 전부 429
                return Ok('{"evidence": "e", "category": "normal"}')
            return RateLimited("0")

        fake = FakeLLM(respond=respond)
        made.append(fake)
        return LLM(cfg_llm, send=fake.send, sleep=nosleep)

    code = await run.amain(_args(tmp_path, "--repeat", "3", config=DEFAULT), make_llm=make_llm)
    assert code == run.EXIT_RATE_LIMITED == 6
    # 3회 × 약 375호출(재시도 포함 약 2,250회)을 다 보내지 않는다
    assert len(made[0].requests) < probes + 150
    out = tmp_path / "runs" / "t"
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert "속도 제한" in summary["invalid"]
    assert "# 무효 실행" in (out / "report.md").read_text(encoding="utf-8")


async def test_scattered_rate_limits_do_not_stop_the_run(tmp_path):
    from fakes import RateLimited

    from guardrail.llm import LLM

    async def nosleep(_s):
        return None

    def make_llm(cfg_llm):
        def respond(kw):  # 열 번에 한 번만 429: 재시도로 풀린다
            if len(fake.requests) % 10 == 0:
                return RateLimited("0")
            if "OutputBaselineOut" in json.dumps(kw.get("response_format", {})):
                return Ok('{"reason": "r", "grounded": true, "safe": true}')
            return Ok('{"evidence": "e", "category": "normal"}')

        fake = FakeLLM(respond=respond)
        return LLM(cfg_llm, send=fake.send, sleep=nosleep)

    assert await run.amain(_args(tmp_path), make_llm=make_llm) == 0


async def test_streak_resets_on_other_errors_and_holds_on_budget():
    from types import SimpleNamespace

    from guardrail.llm import LLMResult

    script: list[str | None] = []

    async def inner(role, messages, schema, *, deadline):
        return LLMResult(None, script.pop(0), 1, 1.0, None, None)  # type: ignore[arg-type]

    async def feed(errors):
        llm = SimpleNamespace(complete=inner, max_calls=100)
        tap = run.Tap()
        tap.attach(llm)  # type: ignore[arg-type]
        script[:] = errors
        for _ in errors:
            await llm.complete("r", [], None, deadline=None)
        return llm, tap

    rl = ["rate_limit"] * (run.RATE_LIMIT_STREAK - 1)
    for last in (None, "timeout"):  # 성공이든 다른 오류든 0으로 되돌린다
        llm, tap = await feed([*rl, last])
        assert tap.streak == 0 and not tap.tripped and llm.max_calls == 100
    llm, tap = await feed([*rl, "budget"])  # budget은 세지도 되돌리지도 않는다
    assert tap.streak == run.RATE_LIMIT_STREAK - 1 and not tap.tripped and llm.max_calls == 100
    script[:] = ["rate_limit"]
    await llm.complete("r", [], None, deadline=None)  # 한 번 더 429면 멈춘다
    assert tap.tripped and llm.max_calls == 0


# --- probe의 맨 요청이 막혀 있으면 측정을 시작하지 않는다 ---


def _blocked_llm(made, step):
    from guardrail.llm import LLM

    async def nosleep(_s):
        return None

    def make_llm(cfg_llm):
        fake = FakeLLM(respond=lambda kw: step)
        made.append(fake)
        return LLM(cfg_llm, send=fake.send, sleep=nosleep)

    return make_llm


async def test_probe_base_rate_limit_stops_before_any_judge_call(tmp_path, capsys):
    from fakes import RateLimited

    made = []
    argv = _args(tmp_path, "--holdout")
    code = await run.amain(
        argv, make_llm=_blocked_llm(made, RateLimited("0")), judge_factory=constant_factory
    )
    err = capsys.readouterr().err
    assert code == run.EXIT_RATE_LIMITED == 6
    assert len(made[0].requests) == 1  # probe의 맨 요청뿐. 판정 호출 0회
    assert "rate_limit" in err and "무효" in err and err.count("\n") == 1
    assert not run.HOLDOUT_VIEWS.exists()  # 열람 기록은 이 종료보다 뒤다


async def test_probe_base_other_failure_uses_its_own_exit_code(tmp_path, capsys):
    from fakes import ServerError

    made = []
    code = await run.amain(
        _args(tmp_path, "--holdout"),
        make_llm=_blocked_llm(made, ServerError()),
        judge_factory=constant_factory,
    )
    err = capsys.readouterr().err
    assert code == run.EXIT_PROBE_FAILED == 7
    assert len(made[0].requests) == 1 and "server" in err and "무효" in err
    assert not run.HOLDOUT_VIEWS.exists()


async def test_probe_success_still_measures_and_records_holdout(tmp_path):
    code = await run.amain(
        _args(tmp_path, "--holdout"), make_llm=_counting_llm, judge_factory=constant_factory
    )
    assert code == 0 and len(run.holdout_views()) == 1


async def test_measure_writes_no_nano_output_for_held_out_rows(monkeypatch):
    # 홀드아웃 행의 nano 파싱 출력(학습자 발화를 옮겨 적은 문장)이 responses.jsonl에 남으면 안 된다
    split = SimpleNamespace(cluster={"a": "c1", "b": "c2"}, holdout=("c2",))
    tap = run.Tap()

    async def fake_run_once(*a, **k):
        tap.parsed += [  # #151부터 (행 id, 호출 기록)이다
            (i, {"role": "input", "parsed": {"evidence": e}}) for i, e in (("a", "x"), ("b", "y"))
        ]
        return SimpleNamespace(
            judgements={i: SimpleNamespace(
                input_source="s", output_tier=None, calls=1, errors=(), knn_major=None,
                lr_pred=None, answer_quote_ok=None, kb_quote_ok=None, nano_refuses=None,
            ) for i in "ab"},
            judge_kind="K",
        )  # fmt: skip

    monkeypatch.setattr(run, "run_once", fake_run_once)
    monkeypatch.setattr(run, "summarize", lambda *a: {})
    monkeypatch.setattr(run, "_responses", lambda out, block: {i: {} for i in out.judgements})
    monkeypatch.setattr(run, "row_points", lambda rows, resp: {})
    monkeypatch.setattr(run, "add_variance", lambda s: None)
    cfg = load_config(BASELINE)
    plan = SimpleNamespace(
        split=split, configs={"c": cfg}, public=[SimpleNamespace(id="a"), SimpleNamespace(id="b")],
        labeled=[],
    )  # fmt: skip
    args = SimpleNamespace(name="t", repeat=1, holdout=True)

    _, records = await run._measure(plan, args, None, tap, None)

    llm_by_id = {r["id"]: r["llm"] for r in records}
    assert llm_by_id["a"] == [{"role": "input", "parsed": {"evidence": "x"}}]
    assert llm_by_id["b"] == []
