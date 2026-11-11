"""모의 부하 시험(TU-17, #79). 저장소 루트에서 `python -m tutor.eval.mock_load`.

운영진 API를 부르지 않는다. 가드 create_app에 튜터를 붙인 앱(통합 IN-05와 같은 조립)을 프로세스
안에서 띄우고, 튜터·가드 요청을 섞어 동시 64로 보낸다(SPEC §3 동시 최대 64). 네트워크도 쓰지 않는다.
- 튜터 LLM은 전송만 가짜로 바꾼다. 파라미터는 서버와 같은 build_llm 값이고, 게이트는 가드 설정
  llm.gate 크기 하나다(서버에서는 가드 LLM의 slot()).
- 가짜 지연은 가드 #105 런박스 실측(가드 판정 호출, 동시 64, p50 4.51초·p95 6.62초)에 맞춘
  로그정규 분포다. 튜터 답변은 출력이 길어 실제로는 더 느릴 수 있다. 429·5xx 재시도는 없다
- 튜터 모드는 baseline으로 고정한다(요청당 nano 1회). 다른 모드는 가짜로 바꾸지 않은 호출이
  생길 수 있다
- 가드 판정기는 상수(constant_factory)라 가드 쪽 LLM 호출은 없다. 가드 판정 부하는 재지 않는다
- httpx ASGITransport로 앱을 바로 부른다. uvicorn·HTTP 파싱은 거치지 않는다
출력은 숫자만 낸다(질문·답변 본문 없음). 가드 접속 로그(본문 없음)는 요청마다 stderr로 나온다.
"""

import argparse
import asyncio
import json
import math
import os
import random
import secrets
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, NamedTuple, TextIO

import httpx
from guardrail.server import constant_factory, create_app

import tutor
from tutor.eval.grader import PUBLIC_DIR, scoring
from tutor.runtime.config import load_config
from tutor.runtime.llm import TutorLLM, build_llm

P50_S, P95_S = 4.51, 6.62  # 가드 #105 런박스 실측(InputOut, 동시 64)
Z95 = 1.6448536269514722  # 표준정규 95백분위
GRADER_TIMEOUT_S = 30.0  # SPEC §3 요청당 30초
CLIENT_TIMEOUT_S = 2 * GRADER_TIMEOUT_S  # 30초를 넘는 응답도 재되, 끝없이 기다리지는 않는다
BASE_URL = "http://localhost"  # 프로세스 안 ASGI 호출(네트워크 없음)
FAKE_ANSWER = json.dumps(
    {"answer": "자료를 바탕으로 답해요.", "action": "answer", "citations": []}, ensure_ascii=False
)


@dataclass(frozen=True)
class Latency:
    """로그정규 지연 분포(초)."""

    mu: float
    sigma: float

    @classmethod
    def from_quantiles(cls, p50: float, p95: float) -> "Latency":
        return cls(mu=math.log(p50), sigma=(math.log(p95) - math.log(p50)) / Z95)

    def sample(self, rng: random.Random) -> float:
        return rng.lognormvariate(self.mu, self.sigma)


class FakeLLM:
    """chat.completions.create 자리. 지연만큼 기다린 뒤 베이스라인 출력 모양의 JSON을 낸다."""

    def __init__(self, latency: Latency, rng: random.Random, *, scale: float) -> None:
        self.latency = latency
        self.rng = rng
        self.scale = scale
        self.calls = 0
        self.active = 0
        self.peak = 0  # 동시에 보낸 호출 최대(게이트 확인)

    async def send(self, _kwargs: dict[str, Any]) -> Any:
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.latency.sample(self.rng) * self.scale)
        finally:
            self.active -= 1
        choice = SimpleNamespace(message=SimpleNamespace(content=FAKE_ANSWER), finish_reason="stop")
        return SimpleNamespace(choices=[choice], usage=SimpleNamespace(completion_tokens=40))


class Reply(NamedTuple):
    endpoint: str
    row_id: str
    took: float  # 초
    status: int  # 0이면 응답 없음(CLIENT_TIMEOUT_S 초과)
    body: Any  # JSON이 아니거나 응답이 없으면 None
    has_candidate: bool


@dataclass(frozen=True)
class Report:
    requests: int
    wall_s: float
    tutor_s: tuple[float, ...]  # 튜터 요청별 지연(초)
    guard_s: tuple[float, ...]
    bad_status: int  # 200이 아닌 응답
    invalid: int  # 채점기 parse_tutor·parse_guard를 못 지나는 본문
    over_limit: int  # 채점기 시간 제한(30초) 이상
    defaults: int  # 튜터 기본값 응답(LLM 실패·시간 부족). 가짜 LLM에서는 0이어야 한다
    covered_tutor_rows: int  # 200 + 규격으로 답한 공개 튜터 행 수
    llm_calls: int
    llm_peak: int
    gate: int  # 쓴 게이트 크기(가드 설정 llm.gate)
    slash_status: int  # POST /tutor/ 상태 코드(3xx면 리다이렉트)


@dataclass(frozen=True)
class Bar:
    p95_s: float = 15.0  # #79 수용 기준(튜터 p95 ≤15초). PRD TU-17은 p50 ≤6초·p95 ≤12초


def _rows(name: str) -> list[dict[str, Any]]:
    lines = (PUBLIC_DIR / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _body(row: dict[str, Any]) -> dict[str, Any]:
    """로컬 채점기와 같은 요청 본문."""
    body = {"spec_version": "1.0", "session_id": "s_" + secrets.token_hex(8)}
    body["messages"] = row["messages"]
    if row.get("candidate_answer"):
        body["candidate_answer"] = row["candidate_answer"]
    return body


def plan(n_tutor: int, n_guard: int, rng: random.Random) -> list[tuple[str, dict[str, Any]]]:
    """공개 세트 행을 돌려 가며 튜터·가드 요청을 만들고 섞는다."""
    tutor_rows, guard_rows = _rows("tutor"), _rows("guard")
    work = [("tutor", tutor_rows[i % len(tutor_rows)]) for i in range(n_tutor)]
    work += [("guard", guard_rows[i % len(guard_rows)]) for i in range(n_guard)]
    rng.shuffle(work)
    return work


def _mock_tutor_llm(state: Any, fake: FakeLLM, overrides: dict[str, Any] | None) -> None:
    """튜터 LLM의 전송만 가짜로 바꾼다. 파라미터·구조화 방식은 서버의 build_llm 결과를 읽는다."""
    cfg = load_config(overrides={**(overrides or {}), "mode": "baseline"})  # 모드는 덮지 못한다
    built = build_llm(state, cfg)
    gate = asyncio.Semaphore(state.cfg.llm.gate)  # 서버에서는 가드 LLM의 slot()(같은 크기)
    state.tutor_cfg = cfg
    state.tutor_llm = TutorLLM(fake.send, lambda: gate, model=built.model, base=built)


async def _one(
    client: httpx.AsyncClient, limit: asyncio.Semaphore, endpoint: str, row: dict[str, Any]
) -> Reply:
    has_candidate = bool(row.get("candidate_answer"))
    async with limit:
        started = time.perf_counter()
        try:
            # ASGITransport는 httpx 시간 제한을 쓰지 않아 직접 건다
            async with asyncio.timeout(CLIENT_TIMEOUT_S):
                resp = await client.post(f"/{endpoint}", json=_body(row))
        except TimeoutError:
            return Reply(endpoint, row["id"], time.perf_counter() - started, 0, None, has_candidate)
        took = time.perf_counter() - started
    try:
        body = resp.json()
    except ValueError:
        body = None
    return Reply(endpoint, row["id"], took, resp.status_code, body, has_candidate)


def _report(
    results: Sequence[Reply], *, wall: float, fake: FakeLLM, gate: int, slash: int
) -> Report:
    tutor_s: list[float] = []
    guard_s: list[float] = []
    bad = invalid = over = defaults = 0
    covered: set[str] = set()
    for r in results:
        bad += r.status != 200
        over += r.took >= GRADER_TIMEOUT_S
        if r.endpoint == "tutor":
            tutor_s.append(r.took)
            ok = scoring.parse_tutor(r.body) is not None
            defaults += r.body == tutor.TUTOR_DEFAULT
            if ok and r.status == 200:
                covered.add(r.row_id)
        else:
            guard_s.append(r.took)
            ok = scoring.parse_guard(r.body, r.has_candidate) is not None
        invalid += not ok
    return Report(
        requests=len(results),
        wall_s=wall,
        tutor_s=tuple(tutor_s),
        guard_s=tuple(guard_s),
        bad_status=bad,
        invalid=invalid,
        over_limit=over,
        defaults=defaults,
        covered_tutor_rows=len(covered),
        llm_calls=fake.calls,
        llm_peak=fake.peak,
        gate=gate,
        slash_status=slash,
    )


async def run_load(
    *,
    n_tutor: int = 300,
    n_guard: int = 300,
    concurrency: int = 64,
    scale: float = 1.0,
    seed: int = 0,
    tutor_overrides: dict[str, Any] | None = None,
) -> Report:
    """TEAM_TOKEN 환경변수가 있어야 한다(앱과 요청이 같은 값을 쓴다)."""
    rng = random.Random(seed)  # noqa: S311 - 모의 지연·순서, 암호용 아님
    fake = FakeLLM(Latency.from_quantiles(P50_S, P95_S), rng, scale=scale)
    app = create_app(
        constant_factory,
        tutor_router=tutor.router,
        defaults={"/tutor": tutor.TUTOR_DEFAULT},
        startups=[tutor.startup],
    )
    headers = {"Authorization": f"Bearer {os.environ['TEAM_TOKEN']}"}
    # 앱 밖으로 나온 예외는 uvicorn처럼 500으로 받아 센다(실행 전체를 멈추지 않는다)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with app.router.lifespan_context(app):
        _mock_tutor_llm(app.state, fake, tutor_overrides)
        async with httpx.AsyncClient(
            transport=transport, base_url=BASE_URL, headers=headers
        ) as client:
            limit = asyncio.Semaphore(concurrency)
            work = plan(n_tutor, n_guard, rng)
            started = time.perf_counter()
            results = await asyncio.gather(*(_one(client, limit, ep, row) for ep, row in work))
            wall = time.perf_counter() - started
            slash = (await client.post("/tutor/", json={})).status_code
        gate = app.state.cfg.llm.gate
    return _report(results, wall=wall, fake=fake, gate=gate, slash=slash)


def _pct(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]


def show(report: Report, bar: Bar, out: TextIO | None = None) -> bool:
    """요약을 내고 수용 기준을 통과했는지 돌려준다."""
    out = out or sys.stdout  # 부를 때의 stdout(기본 인자로 묶지 않는다)
    t50, t95 = _pct(report.tutor_s, 0.5), _pct(report.tutor_s, 0.95)
    tmax, g95 = max(report.tutor_s, default=0.0), _pct(report.guard_s, 0.95)
    out.write(f"요청 {report.requests}개(튜터 {len(report.tutor_s)}·가드 {len(report.guard_s)})")
    out.write(f", 전체 {report.wall_s:.1f}초\n")
    out.write(f"튜터 p50 {t50:.2f}초 · p95 {t95:.2f}초 · 최대 {tmax:.2f}초, 가드 p95 {g95:.2f}초\n")
    out.write(f"200 아님 {report.bad_status}, 규격 밖 {report.invalid}")
    out.write(f", 시간 초과(30초 이상) {report.over_limit}, 튜터 기본값 {report.defaults}\n")
    out.write(f"공개 튜터 행 {report.covered_tutor_rows}개 응답, LLM 호출 {report.llm_calls}회")
    out.write(
        f"(동시 최대 {report.llm_peak}, 게이트 {report.gate}), /tutor/ {report.slash_status}\n"
    )
    failures = []
    if report.bad_status or report.invalid:
        failures.append("형식")
    if report.over_limit:
        failures.append("시간 초과")
    if report.defaults:  # 가짜 LLM인데 기본값이면 LLM 경로가 실패한 것이다
        failures.append("기본값")
    if report.llm_peak > report.gate:
        failures.append("게이트")
    if 300 <= report.slash_status < 400:
        failures.append("리다이렉트")
    if t95 > bar.p95_s:
        failures.append(f"p95 기준 넘음({bar.p95_s:g}초)")
    out.write("통과\n" if not failures else f"실패: {', '.join(failures)}\n")
    return not failures


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="모의 부하 시험(TU-17). 운영진 API를 부르지 않는다")
    ap.add_argument("--tutor", type=int, default=300)
    ap.add_argument("--guard", type=int, default=300)
    ap.add_argument("--concurrency", type=int, default=64)
    ap.add_argument("--scale", type=float, default=1.0, help="가짜 지연 배율(1은 실측 그대로)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--p95", type=float, default=Bar().p95_s, help="튜터 p95 기준(초)")
    args = ap.parse_args(argv)
    os.environ.setdefault("TEAM_TOKEN", secrets.token_hex(16))  # 프로세스 안 앱과 요청이 같이 쓴다
    report = asyncio.run(
        run_load(
            n_tutor=args.tutor,
            n_guard=args.guard,
            concurrency=args.concurrency,
            scale=args.scale,
            seed=args.seed,
        )
    )
    return 0 if show(report, Bar(p95_s=args.p95)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
