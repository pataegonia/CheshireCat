"""모의 부하 시험(TU-17, #79): 운영진 API 없이 튜터·가드를 동시 64로 부른다.

테스트는 가짜 LLM 지연을 줄여(scale) 빠르게 돈다. 실제 시간 실행은 CLI로 한다.
"""

import asyncio
import json
import math
import random

import httpx
import pytest

from tutor.eval import mock_load
from tutor.runtime.response import TUTOR_DEFAULT


@pytest.fixture(autouse=True)
def team_token(monkeypatch):
    monkeypatch.setenv("TEAM_TOKEN", "test-token")


def run(**kw):
    return asyncio.run(mock_load.run_load(**kw))


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
    assert report.fake == pytest.approx((mock_load.P50_S, mock_load.P95_S))


def test_v2_runs_the_real_flow_on_schema_answers():
    """v2 흐름(검색 → 주 호출 → 결정적 수정)이 가짜 전송의 스키마별 정상 답으로 끝까지 간다."""
    report = run(n_tutor=40, n_guard=0, scale=0.001, seed=11)
    assert report.llm_failed == 0 and report.defaults == 0 and report.invalid == 0
    assert report.llm_calls > 0 and dict(report.schemas) == {"MainCallOut": report.llm_calls}


def test_guard_requests_make_their_judge_calls():
    report = run(n_tutor=0, n_guard=10, scale=0.001, seed=12, guard_calls=2)
    assert report.guard_calls == 20 and report.llm_calls == 0  # 가드 요청당 2회


def test_tutor_and_guard_share_one_gate():
    # 동시 128이면 튜터·가드 호출이 게이트를 꽉 채운다. 게이트가 따로였다면 크기를 넘었다.
    # 가짜 지연(약 0.45초)이 요청 128개를 들여보내는 시간보다 길어야 꽉 찬다
    report = run(n_tutor=100, n_guard=100, concurrency=128, scale=0.1, seed=2)
    assert report.llm_peak == report.gate
    assert report.llm_calls > 0 and report.guard_calls > 0


def test_an_unknown_reply_schema_counts_as_an_llm_failure(monkeypatch, capsys):
    """가짜 답을 모르는 호출(예: 새 classify·검증 호출)은 LLM 실패로 드러난다(#88 때 답을 더한다)."""
    monkeypatch.setattr(mock_load, "REPLIES", {})
    report = run(n_tutor=5, n_guard=0, scale=0.001, seed=13)
    assert report.llm_failed > 0 and not mock_load.show(report, mock_load.Bar())
    assert "LLM 실패" in capsys.readouterr().out.splitlines()[-1]


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
    assert report.llm_calls == 80  # 요청당 1회 + 재시도 1회
    assert not mock_load.show(report, mock_load.Bar())  # 기본값뿐인 실행은 빨라도 실패다
    assert capsys.readouterr().out.splitlines()[-1] == "실패: 기본값, LLM 실패"


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
    assert "가정: 튜터 모드 v2, 가짜 LLM 지연 p50 4.51초·p95 6.62초, 가드 판정 요청당 2회" in out
    assert "튜터 20회" in out and "가드 40회" in out  # 역할별 LLM 호출 수
    for reply in mock_load.REPLIES.values():  # 가짜 답 본문도 내지 않는다
        assert json.dumps(reply, ensure_ascii=False) not in out
    assert TUTOR_DEFAULT["answer"] not in out


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


def test_cli_takes_the_fake_latency_and_the_bar(capsys):
    argv = ["--tutor", "10", "--guard", "0", "--scale", "0.001", "--seed", "9"]
    assert mock_load.main([*argv, "--fake-p50", "2", "--fake-p95", "3"]) == 0
    assert (
        "가짜 LLM 지연 p50 2초·p95 3초" in capsys.readouterr().out
    )  # 튜터 주 호출 실측으로 바꿔 끼움
    assert mock_load.main([*argv, "--max-p95", "0"]) == 1
    assert mock_load.main([*argv, "--max-p50", "0"]) == 1


def test_summary_names_each_failure(capsys):
    report = mock_load.Report(
        requests=2,
        wall_s=1.0,
        tutor_s=(1.0,),
        guard_s=(31.0,),
        bad_status=1,
        invalid=1,
        over_limit=1,
        defaults=1,
        llm_failed=1,
        covered_tutor_rows=0,
        llm_calls=70,
        guard_calls=0,
        schemas=(("MainCallOut", 70),),
        llm_peak=65,
        gate=64,
        slash_status=307,
        mode="v2",
        fake=(4.51, 6.62),
        guard_per_request=2,
    )
    assert not mock_load.show(report, mock_load.Bar())
    last = capsys.readouterr().out.splitlines()[-1]
    assert last == "실패: 형식, 시간 초과, 기본값, LLM 실패, 게이트, 리다이렉트"


def test_llm_failures_fail_the_run_even_with_no_default_body(capsys):
    """v2 실패는 TUTOR_DEFAULT와 다른 고정 문장이라 본문으로는 못 센다. LLM 오류로 센다."""
    report = run(n_tutor=5, n_guard=0, scale=0.001, seed=10)
    failed = mock_load.Report(**{**vars(report), "llm_failed": 1})
    assert failed.defaults == 0 and not mock_load.show(failed, mock_load.Bar())
    assert capsys.readouterr().out.splitlines()[-1] == "실패: LLM 실패"


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
        gate=64,
        slash=404,
        llm_failed=0,
        mode="v2",
        guard_per_request=2,
    )
    assert report.invalid == 1 and report.bad_status == int(status != 200)
