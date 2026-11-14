"""모의 부하 시험(TU-17, #79): 운영진 API 없이 튜터·가드를 동시 64로 부른다.

테스트는 가짜 LLM 지연을 줄여(scale) 빠르게 돈다. 실제 시간 실행은 CLI로 한다.
"""

import ast
import asyncio
import contextlib
import dataclasses
import json
import math
import random
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

import httpx
import pytest
from guardrail.schema import OutputVerdict

from integration.contracts import Category
from tutor.eval import mock_load
from tutor.judge import flow, guard_merge
from tutor.judge.conversation import tidy
from tutor.judge.signals import direct_clarify
from tutor.runtime.response import TUTOR_DEFAULT


@pytest.fixture(autouse=True)
def team_token(monkeypatch):
    monkeypatch.setenv("TEAM_TOKEN", "test-token")


def run(**kw):
    return asyncio.run(mock_load.run_load(**kw))


def judged(report, capsys):
    """show()의 판정과 마지막 줄."""
    ok = mock_load.show(report, mock_load.Bar())
    return ok, capsys.readouterr().out.splitlines()[-1]


def passed(**changes):
    """기준 조건(SPEC §3 부하·기본 가정)에서 통과한 실행 요약. changes만 바꾼다."""
    n_tutor, n_guard = mock_load.N_TUTOR, mock_load.N_GUARD
    report = mock_load.Report(
        requests=n_tutor + n_guard,
        wall_s=60.0,
        tutor_s=(4.5,) * n_tutor,
        guard_s=(9.0,) * n_guard,
        bad_status=0,
        invalid=0,
        over_limit=0,
        defaults=0,
        llm_failed=0,
        covered_tutor_rows=200,
        llm_calls=n_tutor,
        llm_requests=n_tutor,
        retried=0,
        guard_calls=2 * n_guard,
        guard_expected=2 * n_guard,
        guard_judged=n_guard,
        guard_timeouts=0,
        guard_limits=mock_load.base_guard_limits(),
        classify_on=True,
        judge_output_on=True,
        classify_calls=n_tutor,
        judge_output_calls=n_tutor,
        guard_skips=(),
        schemas=(("MainCallOut", n_tutor),),
        gate_entries=(("guard", 2 * n_guard), ("tutor", n_tutor)),
        gate_queued=0,
        gate_wait_p95=0.0,
        gate_wait_max=0.0,
        llm_peak=64,
        gate=64,
        server_gate=64,
        slash_status=404,
        mode="v2",
        fake=(mock_load.P50_S, mock_load.P95_S),
        tutor_fake=(mock_load.TUTOR_P50_S, mock_load.TUTOR_P95_S),
        scale=1.0,
        guard_per_request=2,
        concurrency=64,
    )
    return dataclasses.replace(report, **changes)


def test_latency_model_matches_the_runbox_measurement():
    model = mock_load.Latency.from_quantiles(4.51, 6.62)  # 가드 #105 동시 64 실측
    assert math.exp(model.mu) == pytest.approx(4.51)
    assert math.exp(model.mu + mock_load.Z95 * model.sigma) == pytest.approx(6.62)
    samples = sorted(model.sample(random.Random(0)) for _ in range(4000))  # noqa: S311
    assert samples[2000] == pytest.approx(4.51, rel=0.05)


def test_every_request_gets_200_in_contract():
    report = run(n_tutor=300, n_guard=300, scale=0.001, seed=1)
    assert report.requests == 600 and report.bad_status == 0 and report.invalid == 0
    assert report.covered_tutor_rows == 200  # 공개 세트 튜터 200행 전부
    assert report.over_limit == 0 and report.defaults == 0 and report.llm_failed == 0
    assert report.slash_status not in range(300, 400)  # /tutor/ 리다이렉트 없음
    assert report.mode == "v2"  # 기본은 배포 설정과 같은 v2
    assert report.guard_judged == 300
    assert report.guard_calls == report.guard_expected == guard_expected(300, 300, 1)
    assert report.fake == pytest.approx((mock_load.P50_S, mock_load.P95_S))
    assert report.tutor_fake == pytest.approx((mock_load.TUTOR_P50_S, mock_load.TUTOR_P95_S))
    assert report.server_gate == report.gate == 64


def test_tutor_sends_and_guard_judges_wait_on_their_own_latency(monkeypatch):
    """튜터 주 호출(send)과 가드 판정 호출(hold)은 따로 정한 지연 분포를 쓴다."""
    waited: list[float] = []

    async def record(seconds):
        waited.append(seconds)

    monkeypatch.setattr(mock_load.asyncio, "sleep", record)
    fake = mock_load.FakeLLM(
        mock_load.Latency.from_quantiles(2.0, 2.0),
        random.Random(0),  # noqa: S311
        scale=0.5,
        tutor_latency=mock_load.Latency.from_quantiles(8.0, 8.0),
    )
    asyncio.run(fake.hold())
    asyncio.run(fake.send({"response_format": {}}))
    assert waited == pytest.approx([1.0, 4.0])  # 가드 2초·튜터 8초, 배율 0.5
    # 튜터가 부르는 가드 classify·judge_output도 가드 지연을 쓴다(스모크 검사 "가짜 가드 지연")
    guard = mock_load.FakeGuard(fake, mock_load.TimedGate(4), calls=2, read_s=12, budget_s=20)
    asyncio.run(guard.classify([]))
    asyncio.run(guard.judge_output([], "답"))
    assert waited[2:] == pytest.approx([1.0, 1.0])
    same = mock_load.FakeLLM(fake.latency, random.Random(0), scale=1.0)  # noqa: S311
    assert same.tutor_latency is same.latency  # 따로 주지 않으면 가드 판정 지연을 같이 쓴다


REPO = Path(__file__).resolve().parents[2]


def create_app_call(path):
    """파일 안 create_app(...) 호출 하나의 (위치 인자 수, 키워드 인자 AST)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "create_app"
    ]
    assert len(calls) == 1, f"{path}: create_app(...) 호출이 {len(calls)}개"
    return len(calls[0].args), {kw.arg: ast.dump(kw.value) for kw in calls[0].keywords}


def test_mock_load_assembles_the_app_like_the_root_app():
    """모의 부하는 루트 app.py와 같은 인자로 앱을 만들고 가드 판정기만 가짜로 넣는다(#79·#126)."""
    root_args, root_kwargs = create_app_call(REPO / "app.py")
    mock_args, mock_kwargs = create_app_call(REPO / "tutor" / "eval" / "mock_load.py")
    assert root_args == 0 and mock_args == 1  # 루트는 가드 기본 판정기, 모의 부하는 가짜 판정기
    assert set(root_kwargs) == {"tutor_router", "defaults", "startups"}
    assert mock_kwargs == root_kwargs
    # 루트 app.py는 import와 create_app 대입뿐이다(호출 뒤 미들웨어·state·재할당이 붙으면 실패)
    body = ast.parse((REPO / "app.py").read_text(encoding="utf-8")).body
    rest = [
        node
        for node in body
        if not isinstance(node, (ast.Import, ast.ImportFrom))
        and not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
    ]
    assert len(rest) == 1 and isinstance(rest[0], ast.Assign)


def direct_count(n_tutor, seed):
    """plan()이 고른 튜터 행 중 저신호 직접 판정(#98)으로 LLM 없이 끝나는 행 수."""
    work = mock_load.plan(n_tutor, 0, random.Random(seed))  # noqa: S311
    return sum(direct_clarify(tidy(row["messages"])) for _endpoint, row in work)


def test_v2_runs_the_real_flow_on_schema_answers():
    """v2 흐름(검색 → 주 호출 → 결정적 수정)이 가짜 전송의 스키마별 정상 답으로 끝까지 간다."""
    report = run(n_tutor=40, n_guard=0, scale=0.001, seed=11)
    assert report.llm_failed == 0 and report.defaults == 0 and report.invalid == 0
    assert report.llm_calls > 0 and dict(report.schemas) == {"MainCallOut": report.llm_calls}
    # 저신호 직접 판정(#98)을 뺀 튜터 요청은 모두 주 호출을 부른다
    assert report.llm_requests == 40 - direct_count(40, 11)


def guard_expected(n_tutor, n_guard, seed, per_request=mock_load.GUARD_CALLS):
    """plan()이 고른 가드 행의 판정 호출 수 합: 후보 답이 있으면 per_request, 없으면 1."""
    work = mock_load.plan(n_tutor, n_guard, random.Random(seed))  # noqa: S311
    rows = [row for endpoint, row in work if endpoint == "guard"]
    return sum(per_request if row.get("candidate_answer") else 1 for row in rows)


def test_guard_requests_make_their_judge_calls():
    report = run(n_tutor=0, n_guard=10, scale=0.001, seed=12, guard_calls=2)
    assert report.guard_calls == guard_expected(0, 10, 12) and report.guard_judged == 10
    assert report.llm_calls == 0


def test_fake_guard_runs_its_judges_in_parallel_only_with_a_candidate():
    """가드 설계(master-plan §4): 입력·출력 판정을 병렬로, 출력 판정은 후보 답이 있을 때만."""
    fake = mock_load.FakeLLM(
        mock_load.Latency.from_quantiles(1.0, 1.0),
        random.Random(0),  # noqa: S311
        scale=0.01,
    )
    guard = mock_load.FakeGuard(fake, mock_load.TimedGate(4), calls=2, read_s=12, budget_s=20)
    asyncio.run(guard.judge(SimpleNamespace(candidate_answer="답", has_candidate=True)))
    assert fake.guard_calls == 2 and fake.peak == 2  # 둘이 함께 게이트를 쥔다
    asyncio.run(guard.judge(SimpleNamespace(candidate_answer=None, has_candidate=False)))
    assert fake.guard_calls == 3 and guard.judged == 2  # 후보 답이 없으면 입력 판정 1회
    assert guard.timeouts == 0


def slow_guard(latency_s, gate, *, read_s, budget_s):
    fake = mock_load.FakeLLM(
        mock_load.Latency.from_quantiles(latency_s, latency_s),
        random.Random(0),  # noqa: S311
        scale=1.0,
    )
    guard = mock_load.FakeGuard(
        fake, mock_load.TimedGate(gate), calls=2, read_s=read_s, budget_s=budget_s
    )
    return fake, guard


def test_fake_guard_gives_up_on_a_call_past_the_read_limit():
    """가드 LLM은 호출마다 읽기 제한(llm.timeout_s.read)을 넘으면 시간 초과로 끝낸다(#128 리뷰)."""
    fake, guard = slow_guard(0.2, 4, read_s=0.02, budget_s=10)
    with pytest.raises(TimeoutError):
        asyncio.run(guard.judge(SimpleNamespace(candidate_answer="답", has_candidate=True)))
    assert fake.guard_calls == 2 and guard.judged == 0 and guard.timeouts == 1


def test_fake_guard_gives_up_when_the_gate_wait_eats_its_budget():
    """가드 마감(budget_s)은 게이트 대기를 포함한다(가드 LLM.complete의 deadline)."""
    fake, guard = slow_guard(0.05, 1, read_s=10, budget_s=0.07)  # 둘째 호출은 게이트에서 기다린다
    with pytest.raises(TimeoutError):
        asyncio.run(guard.judge(SimpleNamespace(candidate_answer="답", has_candidate=True)))
    assert guard.judged == 0 and guard.timeouts == 1


def test_a_judge_cancelled_from_outside_counts_as_a_guard_timeout():
    """라우트 안전망(budget_s + 5초)이 먼저 끊어도 판정을 못 끝낸 것으로 센다(#128 리뷰)."""
    _fake, guard = slow_guard(1.0, 4, read_s=10, budget_s=10)

    async def cut_short():
        task = asyncio.create_task(guard.judge(SimpleNamespace(candidate_answer="답")))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cut_short())
    assert guard.timeouts == 1 and guard.judged == 0


def test_fake_guard_uses_the_guard_config_limits(monkeypatch):
    from guardrail.config import load_config as load_guard_config

    seen = {}
    real = mock_load.FakeGuard

    def capture(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(mock_load, "FakeGuard", capture)
    report = run(n_tutor=1, n_guard=1, scale=0.001, seed=23)
    cfg = load_guard_config()
    assert seen == {"read_s": cfg.llm.timeout_s.read, "budget_s": cfg.budget_s}
    assert report.guard_limits == (cfg.llm.timeout_s.read, cfg.budget_s)


def test_base_guard_limits_are_the_default_guard_config(monkeypatch, tmp_path):
    """GUARD_CONFIG로 다른 설정을 줘도 기준은 가드 기본 설정이다."""
    from guardrail.config import DEFAULT_CONFIG
    from guardrail.config import load_config as load_guard_config

    base = load_guard_config(DEFAULT_CONFIG)
    monkeypatch.setenv("GUARD_CONFIG", str(tmp_path / "없는 파일.yaml"))
    assert mock_load.base_guard_limits() == (base.llm.timeout_s.read, base.budget_s)


def test_guard_timeouts_fail_the_run(monkeypatch, capsys):
    real = mock_load.FakeGuard

    def quick_limits(fake, gate, calls, **_limits):
        return real(fake, gate, calls, read_s=0.01, budget_s=20)

    monkeypatch.setattr(mock_load, "FakeGuard", quick_limits)
    report = run(n_tutor=2, n_guard=5, scale=0.05, seed=24)  # 가짜 지연 약 0.2초 > 0.01초
    assert report.guard_timeouts == 5 and report.guard_judged == 0
    assert report.invalid == 0  # 라우트가 200 기본값으로 바꿔 형식 검사로는 안 잡힌다
    ok, last = judged(report, capsys)
    assert not ok and "가드 판정" in last and "가드 시간 초과" in last


@pytest.mark.parametrize(
    "flag",
    [
        ["--gate", "0"],
        ["--gate", "-1"],
        ["--concurrency", "0"],
        ["--guard-calls", "-1"],
        ["--scale", "0"],
        ["--guard-p50", "0"],
        ["--guard-p50", "3", "--guard-p95", "2"],
        ["--tutor-p50", "0"],
        ["--tutor-p50", "9", "--tutor-p95", "8"],
        ["--fake-p50", "9"],  # 옛 이름(튜터에도 걸리던 값)은 조용히 다른 실행이 되지 않게 멈춘다
    ],
)
def test_cli_rejects_values_that_hang_or_break_the_run(flag, capsys):
    with pytest.raises(SystemExit) as stop:
        mock_load.main(["--tutor", "1", "--guard", "0", *flag])
    assert stop.value.code == 2


def test_tutor_and_guard_calls_all_go_through_one_gate():
    report = run(n_tutor=20, n_guard=20, scale=0.001, seed=2)
    stages = report.classify_calls + report.judge_output_calls  # 튜터가 부른 가드 단계도 가드 몫
    assert stages > 0
    assert dict(report.gate_entries) == {
        "tutor": report.llm_calls,
        "guard": report.guard_calls + stages,
    }


def test_tutor_calls_the_guard_classify_and_judge_output_once_per_llm_request():
    """#138: v2는 가드 classify(주 호출과 병렬)·judge_output(주 호출 뒤)을 부른다(#159 리뷰)."""
    report = run(n_tutor=40, n_guard=0, scale=0.001, seed=11)
    assert report.classify_on and report.judge_output_on  # config/tutor.yaml: parallel·guard
    # 가짜 주 호출은 늘 normal·answer라 LLM을 부른 요청은 모두 judge_output까지 간다
    assert report.classify_calls == report.judge_output_calls == report.llm_requests
    assert report.llm_requests == 40 - direct_count(40, 11)  # 직접 판정은 셋 다 0회
    assert report.guard_skips == ()  # 가짜 가드의 반환 형식을 튜터가 받는다(invalid·error 없음)
    assert dict(report.gate_entries)["guard"] == 2 * report.llm_requests


def test_judge_output_waits_for_the_main_call_and_classify_runs_alongside(monkeypatch):
    events: list[str] = []
    send, stage = mock_load.FakeLLM.send, mock_load.FakeGuard._stage_call

    async def logged_send(self, kwargs):
        events.append("main start")
        try:
            return await send(self, kwargs)
        finally:
            events.append("main end")

    async def logged_stage(self, name):
        events.append(f"{name} start")
        await stage(self, name)

    monkeypatch.setattr(mock_load.FakeLLM, "send", logged_send)
    monkeypatch.setattr(mock_load.FakeGuard, "_stage_call", logged_stage)
    assert direct_count(1, 0) == 0  # 첫 튜터 행은 LLM을 부른다
    run(n_tutor=1, n_guard=0, scale=0.001, seed=0)
    assert events == ["classify start", "main start", "main end", "judge_output start"]


def test_fake_guard_stages_hold_the_shared_gate_and_answer_like_the_guard():
    fake = mock_load.FakeLLM(
        mock_load.Latency.from_quantiles(1.0, 1.0),
        random.Random(0),  # noqa: S311
        scale=0.01,
    )
    gate = mock_load.TimedGate(1)
    guard = mock_load.FakeGuard(fake, gate, calls=2, read_s=12, budget_s=20)

    async def both():
        return await asyncio.gather(guard.classify([]), guard.judge_output([], "답", "normal"))

    category, verdict = asyncio.run(both())
    assert gate.entries == {"guard": 2} and gate.queued == 1 and fake.peak == 1
    assert guard.stage_calls == {"classify": 1, "judge_output": 1}
    assert category in get_args(Category) and category not in guard_merge.ATTACK_CATEGORIES
    assert isinstance(verdict, OutputVerdict) and verdict.safe and verdict.grounded
    assert fake.guard_calls == 0  # /guard 판정 호출 수와 따로 센다


def test_fake_guard_stages_give_up_past_the_read_limit():
    _fake, guard = slow_guard(0.2, 4, read_s=0.02, budget_s=10)
    for call in (guard.classify([]), guard.judge_output([], "답")):
        with pytest.raises(TimeoutError):  # 튜터는 이것을 그 단계 timeout으로 센다
            asyncio.run(call)
    assert guard.stage_calls == {"classify": 1, "judge_output": 1}


@pytest.mark.parametrize(
    "missing, reasons",
    [
        (("classify",), "가드 classify 안 부름"),
        (("judge_output",), "가드 judge_output 안 부름"),
        (("classify", "judge_output"), "가드 classify 안 부름, 가드 judge_output 안 부름"),
    ],
)
def test_a_guard_without_the_tutor_stages_fails_the_run(monkeypatch, capsys, missing, reasons):
    """#159 리뷰: 가짜 가드에 없으면 튜터가 건너뛰어(missing) 주 호출만 잰다. 통과로 내지 않는다."""
    for name in missing:
        monkeypatch.delattr(mock_load.FakeGuard, name)
    report = run(n_tutor=10, n_guard=0, scale=0.001, seed=15)
    stage = {"classify": "classify", "judge_output": "judge"}  # guard_merge의 단계 이름
    skips = dict(report.guard_skips)
    assert report.llm_requests > 0
    assert all(skips[f"{stage[name]} missing"] == report.llm_requests for name in missing)
    ok, last = judged(report, capsys)
    assert not ok and last == f"실패: {reasons}"


def test_gate_waits_are_measured_when_the_gate_is_tight():
    # 게이트 2에 요청 20개를 한꺼번에 보내면 호출이 줄을 선다(가짜 지연 약 0.045초)
    report = run(n_tutor=10, n_guard=10, concurrency=20, gate=2, scale=0.01, seed=14)
    assert report.gate == 2 and report.llm_peak <= 2 and report.server_gate == 64
    assert report.gate_queued > 0 and report.gate_wait_max > 0.02 and report.gate_wait_p95 > 0.0


def test_a_tutor_call_that_skips_the_gate_fails_the_run(monkeypatch, capsys):
    real = mock_load._mock_tutor_llm

    def separate_gate(state, fake, overrides, mode):
        real(state, fake, overrides, mode)
        state.tutor_llm.gate = contextlib.nullcontext  # 공유 게이트를 건너뛴다

    monkeypatch.setattr(mock_load, "_mock_tutor_llm", separate_gate)
    report = run(n_tutor=10, n_guard=0, scale=0.001, seed=15)
    ok, last = judged(report, capsys)
    assert not ok and last == "실패: 게이트 우회"


@pytest.mark.parametrize(
    "broken",
    [
        "raise",  # 판정기 예외는 가드 라우트가 200 기본값으로 바꾼다
        "skip",  # 판정 호출 없이 바로 판정
    ],
)
def test_a_broken_guard_judge_fails_the_run(monkeypatch, capsys, broken):
    async def judge(self, req):
        if broken == "raise":
            raise RuntimeError("판정기 고장")
        return await self.inner.judge(req)

    monkeypatch.setattr(mock_load.FakeGuard, "judge", judge)
    report = run(n_tutor=5, n_guard=5, scale=0.001, seed=16)
    assert report.invalid == 0  # 기본값도 규격 응답이라 형식 검사로는 안 잡힌다
    ok, last = judged(report, capsys)
    assert not ok and "가드 판정" in last


def test_a_tutor_that_never_calls_the_llm_fails_the_run(monkeypatch, capsys):
    """v2 고정 문장은 라우터 기본값과 달라 본문으로는 못 센다. LLM을 부른 요청 수로 센다."""

    async def no_llm(req, state, *, deadline):
        return flow.fallback_body()

    monkeypatch.setattr(flow, "answer_v2", no_llm)
    report = run(n_tutor=10, n_guard=0, scale=0.001, seed=17)
    assert report.defaults == 0 and report.llm_failed == 0 and report.llm_requests == 0
    ok, last = judged(report, capsys)
    assert not ok and last == "실패: LLM 경로 적음"


def test_unexpected_reply_schemas_fail_the_run(monkeypatch, capsys):
    monkeypatch.setitem(mock_load.EXPECTED_SCHEMAS, "v2", frozenset({"Other"}))
    report = run(n_tutor=5, n_guard=0, scale=0.001, seed=18)
    ok, last = judged(report, capsys)
    assert not ok and last == "실패: 스키마"


def test_a_run_without_tutor_requests_fails(capsys):
    report = run(n_tutor=0, n_guard=5, scale=0.001, seed=19)
    ok, last = judged(report, capsys)
    assert not ok and last == "실패: 튜터 요청 없음"


def test_an_unknown_reply_schema_counts_as_an_llm_failure(monkeypatch, capsys):
    """가짜 답을 모르는 호출(예: 튜터 LLM의 새 호출)은 LLM 실패로 드러난다."""
    monkeypatch.setattr(mock_load, "REPLIES", {})
    report = run(n_tutor=5, n_guard=0, scale=0.001, seed=13)
    ok, last = judged(report, capsys)
    assert report.llm_failed > 0 and not ok and "LLM 실패" in last


def test_refuses_a_config_that_would_call_the_embedding_api(monkeypatch):
    """기동이 임베딩 검색을 만들면 운영진 임베딩 API를 부르게 된다(가짜 임베딩은 아직 없다)."""
    real = mock_load.load_config

    def embedding_on(overrides=None):
        return real(overrides={**(overrides or {}), "toggles.embedding": True})

    monkeypatch.setattr(mock_load, "load_config", embedding_on)
    with pytest.raises(RuntimeError, match="임베딩"):
        run(n_tutor=1, n_guard=0, scale=0.001, seed=22)


def test_refuses_overrides_that_turn_on_embedding():
    """덮어쓰기로 켠 임베딩은 기동이 만들지 않아 임베딩 없이 재게 된다(#128 리뷰 재현)."""
    with pytest.raises(RuntimeError, match="임베딩"):
        run(n_tutor=1, n_guard=0, scale=0.001, seed=22, tutor_overrides={"toggles.embedding": True})


def test_tutor_overrides_cannot_change_the_mode():
    report = run(
        n_tutor=5, n_guard=0, scale=0.001, seed=7, mode="baseline", tutor_overrides={"mode": "v2"}
    )
    assert report.mode == "baseline" and dict(report.schemas) == {"BaselineOut": 5}


def test_call_timeouts_answer_with_the_default_and_fail_the_bar(capsys):
    report = run(
        n_tutor=40,
        n_guard=0,
        scale=0.05,  # 가짜 지연 약 0.2초 > 호출 제한 0.05초
        seed=3,
        mode="baseline",  # 베이스라인 실패는 라우터 기본값(TUTOR_DEFAULT) 본문이다
        tutor_overrides={"llm.timeout_s.main": 0.05},
    )
    assert report.bad_status == 0 and report.invalid == 0 and report.over_limit == 0
    assert report.defaults == 40  # 재시도까지 시간 제한에 걸리면 TU-12 기본값 200
    assert report.llm_failed == 40  # 응답 문구와 상관없이 LLM 오류로 센다(v2 고정 문장도 잡힘)
    assert report.llm_calls == 80 and report.retried == 40  # 요청당 1회 + 재시도 1회
    ok, last = judged(report, capsys)
    assert not ok and last == "실패: 기본값, LLM 실패"  # 기본값뿐인 실행은 빨라도 실패다


def test_requests_past_the_client_limit_are_cut_and_counted(monkeypatch):
    # 실제 앱 안에서 요청을 끊어도 실행이 멈추지 않고, 끊긴 요청은 응답 없음(상태 0)으로 센다
    monkeypatch.setattr(mock_load, "CLIENT_TIMEOUT_S", 0.05)
    report = run(n_tutor=10, n_guard=0, scale=0.05, seed=8)  # 가짜 지연 약 0.2초 > 0.05초
    assert report.bad_status == 10 and report.invalid == 10 and report.defaults == 0
    assert report.covered_tutor_rows == 0 and report.over_limit == 0


def test_summary_prints_numbers_only_and_judges_the_bar(capsys):
    report = run(n_tutor=20, n_guard=20, scale=0.001, seed=4)
    ok = mock_load.show(report, mock_load.Bar())
    out = capsys.readouterr().out
    assert ok and "튜터 p50" in out and "시간 초과(30초 이상) 0" in out
    assert (
        "가정: 튜터 모드 v2, 가짜 지연 튜터 주 호출 p50 4.9초·p95 8.3초"
        "·가드 판정 p50 4.51초·p95 6.62초(배율 0.001)"
        ", 가드 판정 최대 2회 병렬(읽기 제한 12초·마감 20초), 게이트 64, 동시 64\n" in out
    )
    assert f"튜터 {report.llm_calls}회" in out  # 튜터는 직접 판정을 뺀 수
    assert f"가드 {report.guard_calls}회" in out and report.guard_calls == report.guard_expected
    assert report.llm_calls >= mock_load.MIN_LLM_SHARE * 20  # 출력 숫자 자체도 하한을 지킨다
    n = report.llm_requests  # 튜터가 부른 가드 classify·judge_output(배율 0.001이라 시간 초과 없음)
    assert f"튜터의 가드 호출 classify {n}회 · judge_output {n}회(건너뜀 없음)\n" in out
    # 튜터 요청이 classify·주 호출을 함께 쥐어 요청 40개로도 게이트 64가 찰 수 있다
    assert f"게이트 줄 선 호출 {report.gate_queued}회" in out
    assert out.splitlines()[-1] == (  # 배율·요청 수가 기준과 달라 실측이 아니다
        "통과(스모크: 배율 0.001, 튜터 요청 20 < 300, 가드 요청 20 < 300, 실측 아님)"
    )
    for reply in mock_load.REPLIES.values():  # 가짜 답 본문도 내지 않는다
        assert json.dumps(reply, ensure_ascii=False) not in out
    assert TUTOR_DEFAULT["answer"] not in out


def test_a_real_time_pass_names_the_bar_and_says_when_the_gate_never_filled(capsys):
    ok, last = judged(passed(), capsys)
    assert ok and last == "통과(튜터 p50 ≤6초·p95 ≤12초, 게이트 경합 없음)"
    ok, last = judged(passed(gate_queued=3), capsys)
    assert ok and last == "통과(튜터 p50 ≤6초·p95 ≤12초)"


@pytest.mark.parametrize(
    "changes, reason",
    [
        ({"scale": 0.5}, "배율 0.5"),
        ({"fake": (0.1, 0.2)}, "가짜 가드 지연이 기본보다 짧음"),
        ({"fake": (mock_load.P50_S, 6.0)}, "가짜 가드 지연이 기본보다 짧음"),
        ({"tutor_fake": (mock_load.P50_S, mock_load.P95_S)}, "가짜 튜터 지연이 기본보다 짧음"),
        ({"tutor_fake": (mock_load.TUTOR_P50_S, 8.0)}, "가짜 튜터 지연이 기본보다 짧음"),
        (
            {"guard_per_request": 1, "guard_calls": 300, "guard_expected": 300},
            "가드 판정 최대 1회 < 2회",
        ),
        ({"gate": 128}, "게이트 128 > 서버 64"),
        ({"guard_limits": (1000.0, 20.0)}, "가드 시간 제한이 기본보다 김"),
        ({"guard_limits": (12.0, 1000.0)}, "가드 시간 제한이 기본보다 김"),
        ({"concurrency": 2}, "동시 2 < 64"),
        ({"tutor_s": (4.5,) * 5}, "튜터 요청 5 < 300"),
        (
            {"guard_s": (), "guard_judged": 0, "guard_calls": 0, "guard_expected": 0},
            "가드 요청 0 < 300",
        ),
    ],
)
def test_an_easier_run_passes_only_as_a_smoke_run(capsys, changes, reason):
    """SPEC §3 부하나 기본 가정보다 쉬운 조건의 통과는 실측 통과로 내지 않는다."""
    ok, last = judged(passed(**changes), capsys)
    assert ok and last == f"통과(스모크: {reason}, 실측 아님)"


@pytest.mark.parametrize(
    "changes",
    [
        {"fake": (8.0, 14.0)},  # 가드 판정 호출이 더 느리면 더 어렵다
        {"tutor_fake": (8.0, 14.0)},  # 동시 64 튜터 주 호출 실측이 더 느리면 바꿔 끼운다
        {"gate": 32, "llm_peak": 32, "gate_queued": 100},
        {"concurrency": 128},
        {"guard_limits": (6.0, 10.0)},  # 가드 제한이 더 짧으면 더 어렵다
        {
            "guard_per_request": 3,
            "guard_calls": 3 * mock_load.N_GUARD,
            "guard_expected": 3 * mock_load.N_GUARD,
            "gate_entries": (("guard", 3 * mock_load.N_GUARD), ("tutor", mock_load.N_TUTOR)),
        },
    ],
)
def test_a_harder_run_keeps_the_real_time_label(capsys, changes):
    ok, last = judged(passed(**changes), capsys)
    assert ok and last.startswith("통과(튜터 p50 ≤6초·p95 ≤12초")


def test_cli_passes_easy_inputs_only_as_a_smoke_run(capsys):
    """검증 리뷰 재현: 배율이 1이어도 부하·지연을 줄인 통과는 스모크다."""
    argv = ["--tutor", "5", "--guard", "0", "--concurrency", "2", "--seed", "21"]
    argv += ["--guard-p50", "0.1", "--guard-p95", "0.2", "--guard-calls", "0"]
    assert mock_load.main(argv) == 0
    assert capsys.readouterr().out.splitlines()[-1] == (
        "통과(스모크: 가짜 가드 지연이 기본보다 짧음, 가드 판정 최대 0회 < 2회, 동시 2 < 64"
        ", 튜터 요청 5 < 300, 가드 요청 0 < 300, 실측 아님)"
    )


def test_a_run_longer_than_the_wall_limit_fails(capsys):
    """SPEC §3: 5분을 기다린 채점은 끊겼고 한계는 더 짧을 수 있다. 채점기 판정 몫을 남긴다."""
    limit = mock_load.WALL_LIMIT_S
    assert limit <= 300 / 2
    ok, last = judged(passed(wall_s=limit + 1), capsys)
    assert not ok and last == f"실패: 전체 {limit:g}초 넘음"
    ok, _last = judged(passed(wall_s=limit), capsys)
    assert ok


@pytest.mark.parametrize(
    "changes",
    [{"scale": 2.0}, {"concurrency": 8}],  # 배율·낮은 동시는 전체 시간을 부풀린다(스모크)
)
def test_the_wall_limit_applies_only_to_real_time_load(capsys, changes):
    ok, last = judged(passed(wall_s=mock_load.WALL_LIMIT_S * 3, **changes), capsys)
    assert ok and last.startswith("통과(스모크")


def test_default_bar_is_the_prd_latency_target():
    assert mock_load.Bar() == mock_load.Bar(p50_s=6.0, p95_s=12.0)  # TU-13·TU-17, 통합 v1.1


@pytest.mark.parametrize(
    "bar, missed",
    [(mock_load.Bar(p95_s=0.0), "p95 기준 넘음"), (mock_load.Bar(p50_s=0.0), "p50 기준 넘음")],
)
def test_summary_fails_when_the_bar_is_missed(capsys, bar, missed):
    report = run(n_tutor=20, n_guard=0, scale=0.001, seed=5)
    assert not mock_load.show(report, bar)  # p50·p95 중 하나라도 기준을 넘으면 실패
    assert missed in capsys.readouterr().out


def test_cli_runs_fast_and_returns_zero(capsys):
    argv = ["--tutor", "20", "--guard", "20", "--scale", "0.001", "--seed", "6"]
    assert mock_load.main(argv) == 0
    assert "요청 40개" in capsys.readouterr().out


def test_cli_takes_the_fake_latency_the_bar_and_the_gate(capsys):
    argv = ["--tutor", "10", "--guard", "0", "--scale", "0.001", "--seed", "9"]
    latency = ["--guard-p50", "2", "--guard-p95", "3", "--tutor-p50", "5", "--tutor-p95", "9"]
    assert mock_load.main([*argv, *latency, "--gate", "8"]) == 0
    out = capsys.readouterr().out
    assert "튜터 주 호출 p50 5초·p95 9초·가드 판정 p50 2초·p95 3초" in out and "게이트 8" in out
    assert mock_load.main([*argv, "--max-p95", "0"]) == 1
    assert mock_load.main([*argv, "--max-p50", "0"]) == 1


def test_summary_names_each_failure(capsys):
    report = passed(
        requests=2,
        tutor_s=(1.0,),
        guard_s=(31.0,),
        bad_status=1,
        invalid=1,
        over_limit=1,
        defaults=1,
        llm_failed=1,
        covered_tutor_rows=0,
        llm_calls=70,
        llm_requests=0,
        guard_calls=0,
        guard_judged=0,
        schemas=(("Other", 70),),
        gate_entries=(("tutor", 1),),
        llm_peak=65,
        slash_status=307,
    )
    ok, last = judged(report, capsys)
    assert not ok
    assert last == (
        "실패: 형식, 시간 초과, 기본값, LLM 실패, LLM 경로 적음, 스키마, 가드 판정, 게이트 우회"
        ", 게이트, 리다이렉트"
    )


def test_llm_failures_fail_the_run_even_with_no_default_body(capsys):
    """v2 실패는 TUTOR_DEFAULT와 다른 고정 문장이라 본문으로는 못 센다. LLM 오류로 센다."""
    report = run(n_tutor=5, n_guard=0, scale=0.001, seed=10)
    failed = mock_load.Report(**{**vars(report), "llm_failed": 1})
    ok, last = judged(failed, capsys)
    assert failed.defaults == 0 and not ok and last == "실패: LLM 실패"


class _Hang:
    async def post(self, *_args, **_kwargs):
        await asyncio.sleep(3600)


class _Text:
    async def post(self, *_args, **_kwargs):
        return httpx.Response(200, text="not json")


@pytest.mark.parametrize("client, status", [(_Hang(), 0), (_Text(), 200)])
def test_missing_or_non_json_responses_count_as_failures(client, status, monkeypatch):
    monkeypatch.setattr(mock_load, "CLIENT_TIMEOUT_S", 0.01)  # 응답이 없으면 여기서 끊는다
    row = {"id": "r", "messages": [{"role": "user", "content": "q"}]}
    reply = asyncio.run(mock_load._one(client, asyncio.Semaphore(1), "tutor", row))
    assert reply.status == status and reply.body is None
    fake = mock_load.FakeLLM(mock_load.Latency(0.0, 0.0), random.Random(0), scale=0)  # noqa: S311
    report = mock_load._report(
        [reply],
        wall=1.0,
        fake=fake,
        gate=mock_load.TimedGate(64),
        llm=SimpleNamespace(completes=0, failed=0, retried=0),
        guard_judged=0,
        guard_timeouts=0,
        guard_limits=(12.0, 20.0),
        stages_on=(True, True),
        stage_calls={},
        guard_skips={},
        slash=404,
        mode="v2",
        guard_per_request=2,
        concurrency=1,
        server_gate=64,
    )
    assert report.invalid == 1 and report.bad_status == int(status != 200)
