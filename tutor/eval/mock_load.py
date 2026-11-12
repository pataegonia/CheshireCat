"""모의 부하 시험(TU-17, #79). 저장소 루트에서 `python -m tutor.eval.mock_load`.

운영진 API를 부르지 않는다. 가드 create_app에 튜터를 붙인 앱(통합 IN-05와 같은 조립)을 프로세스
안에서 띄우고, 튜터·가드 요청을 섞어 동시 64로 보낸다(SPEC §3 동시 최대 64). 네트워크도 쓰지 않는다.
- 튜터 LLM은 전송만 가짜로 바꾼다. 파라미터는 서버와 같은 build_llm 값이다. 가짜 전송은 응답
  스키마 이름(REPLIES)에 맞는 최소 정상 JSON을 내므로, v2는 검색 → 주 호출 → 결정적 수정까지
  실제 흐름을 탄다. 답을 모르는 스키마(새 호출)는 빈 객체라 LLM 실패로 드러난다(#88 때 더한다)
- 튜터 모드는 `mode`(기본 v2, 배포 설정과 같음)로 정한다. 지금 v2는 요청당 주 호출 1회다
- 가드 판정기는 가짜다. 가드 요청마다 nano `guard_calls`회(기본 2)를 차례로 흉내 내며, 튜터 LLM과
  같은 게이트(가드 설정 llm.gate 크기 하나, 서버에서는 가드 LLM의 slot())를 쥔다. 판정은 상수다
- 가짜 지연은 로그정규 분포다. 기본은 가드 #105 런박스 실측(가드 판정 호출, 동시 64, p50 4.51초·
  p95 6.62초)이고 튜터·가드 호출에 같이 쓴다. 튜터 주 호출은 출력이 길어 더 느릴 수 있으니,
  실측이 나오면 `--fake-p50`·`--fake-p95`로 바꿔 끼운다. 429·5xx 재시도는 없다
- httpx ASGITransport로 앱을 바로 부른다. uvicorn·HTTP 파싱은 거치지 않는다
- 합격선은 튜터 p50 ≤6초·p95 ≤12초(TU-13·TU-17)다. LLM 실패는 응답 문구가 아니라 튜터 LLM이
  오류로 끝낸 호출 수로 센다(모드·고정 문장과 상관없다)
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
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, NamedTuple, TextIO

import httpx
from guardrail.guard import ConstantGuard
from guardrail.server import create_app

import tutor
from tutor.eval.grader import PUBLIC_DIR, scoring
from tutor.runtime.config import load_config
from tutor.runtime.llm import Result, TutorLLM, build_llm

P50_S, P95_S = 4.51, 6.62  # 가드 #105 런박스 실측(InputOut, 동시 64)
Z95 = 1.6448536269514722  # 표준정규 95백분위
GRADER_TIMEOUT_S = 30.0  # SPEC §3 요청당 30초
CLIENT_TIMEOUT_S = 2 * GRADER_TIMEOUT_S  # 30초를 넘는 응답도 재되, 끝없이 기다리지는 않는다
BASE_URL = "http://localhost"  # 프로세스 안 ASGI 호출(네트워크 없음)
GUARD_CALLS = 2  # 가드 요청당 nano 판정 호출 수(입력·출력 판정)
# 응답 스키마 이름별 가짜 정상 답(과목 사실 없음). 없는 이름은 빈 객체라 bad_json으로 센다
REPLIES: dict[str, dict[str, Any]] = {
    "BaselineOut": {"answer": "자료를 바탕으로 답해요.", "action": "answer", "citations": []},
    "MainCallOut": {
        "category": "normal",
        "action": "answer",
        "sentences": [{"text": "자료를 바탕으로 답해요.", "kind": "non_claim", "evidence": []}],
    },
}


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

    def quantiles(self) -> tuple[float, float]:
        """(p50, p95) 초."""
        return math.exp(self.mu), math.exp(self.mu + Z95 * self.sigma)


def _schema_name(kwargs: dict[str, Any]) -> str:
    """요청한 응답 스키마 이름. json_object 모드는 이름이 없어 "?"다(답을 모르는 호출)."""
    fmt = kwargs.get("response_format") or {}
    return str((fmt.get("json_schema") or {}).get("name", "?"))


class FakeLLM:
    """chat.completions.create 자리. 지연만큼 기다린 뒤 응답 스키마에 맞는 가짜 정상 답을 낸다.

    가드 판정 호출도 같은 지연 분포로 흉내 낸다(hold). peak는 튜터·가드 호출을 합쳐 동시에 진행
    중인 호출의 최대다(게이트 확인).
    """

    def __init__(self, latency: Latency, rng: random.Random, *, scale: float) -> None:
        self.latency = latency
        self.rng = rng
        self.scale = scale
        self.calls = 0  # 튜터 LLM 전송 수
        self.guard_calls = 0  # 가드 판정 호출 수
        self.schemas: Counter[str] = Counter()  # 튜터 전송의 응답 스키마별 수
        self.active = 0
        self.peak = 0

    async def hold(self) -> None:
        """호출 하나만큼 기다린다."""
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.latency.sample(self.rng) * self.scale)
        finally:
            self.active -= 1

    async def send(self, kwargs: dict[str, Any]) -> Any:
        self.calls += 1
        name = _schema_name(kwargs)
        self.schemas[name] += 1
        await self.hold()
        content = json.dumps(REPLIES.get(name, {}), ensure_ascii=False)
        choice = SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")
        return SimpleNamespace(choices=[choice], usage=SimpleNamespace(completion_tokens=40))


class FakeGuard:
    """가드 판정기 자리. 요청마다 nano 호출 calls회를 공유 게이트를 쥔 채 차례로 흉내 낸다.

    판정 자체는 상수(ConstantGuard)다. 부하만 서버의 가드와 비슷하게 만든다.
    """

    def __init__(self, fake: FakeLLM, gate: asyncio.Semaphore, calls: int) -> None:
        self.fake = fake
        self.gate = gate  # 튜터 LLM도 이 게이트를 쓴다(서버에서는 가드 LLM의 slot())
        self.calls = calls
        self.inner = ConstantGuard()

    async def judge(self, req: Any) -> Any:
        for _ in range(self.calls):
            async with self.gate:
                self.fake.guard_calls += 1
                await self.fake.hold()
        return await self.inner.judge(req)


class CountingLLM(TutorLLM):
    """오류로 끝난 호출(시간 초과·게이트 대기 초과·깨진 JSON 등)을 센다."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.failed = 0

    async def complete(self, *args: Any, **kwargs: Any) -> Result:
        result = await super().complete(*args, **kwargs)
        if result.error is not None:
            self.failed += 1
        return result


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
    defaults: int  # 라우터 기본값 본문(TUTOR_DEFAULT)과 같은 응답. 가짜 LLM에서는 0이어야 한다
    llm_failed: int  # 튜터 LLM 호출 중 오류로 끝난 것. 가짜 LLM에서는 0이어야 한다
    covered_tutor_rows: int  # 200 + 규격으로 답한 공개 튜터 행 수
    llm_calls: int  # 튜터 LLM 전송 수
    guard_calls: int  # 가드 판정 호출 수
    schemas: tuple[tuple[str, int], ...]  # 튜터 전송의 응답 스키마별 수
    llm_peak: int  # 튜터·가드 호출을 합쳐 동시에 진행 중인 최대
    gate: int  # 쓴 게이트 크기(가드 설정 llm.gate)
    slash_status: int  # POST /tutor/ 상태 코드(3xx면 리다이렉트)
    mode: str  # 튜터 모드
    fake: tuple[float, float]  # 가짜 LLM 지연 (p50, p95) 초
    guard_per_request: int  # 가드 요청당 판정 호출 수(가정)


@dataclass(frozen=True)
class Bar:
    p50_s: float = 6.0  # PRD TU-13·TU-17, 통합 PRD v1.1: 튜터 p50 ≤6초
    p95_s: float = 12.0  # 튜터 p95 ≤12초


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


def _mock_tutor_llm(state: Any, fake: FakeLLM, overrides: dict[str, Any] | None, mode: str) -> None:
    """튜터 LLM의 전송만 가짜로 바꾼다. 파라미터·구조화 방식은 서버의 build_llm 결과를 읽는다.

    모드는 mode 인자로만 정한다(overrides의 mode는 덮인다). 게이트는 가짜 가드와 같은 것이다.
    """
    cfg = load_config(overrides={**(overrides or {}), "mode": mode})
    built = build_llm(state, cfg)
    gate = state.guard.gate
    state.tutor_cfg = cfg
    state.tutor_llm = CountingLLM(fake.send, lambda: gate, model=built.model, base=built)


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
    results: Sequence[Reply],
    *,
    wall: float,
    fake: FakeLLM,
    gate: int,
    slash: int,
    llm_failed: int,
    mode: str,
    guard_per_request: int,
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
        llm_failed=llm_failed,
        covered_tutor_rows=len(covered),
        llm_calls=fake.calls,
        guard_calls=fake.guard_calls,
        schemas=tuple(sorted(fake.schemas.items())),
        llm_peak=fake.peak,
        gate=gate,
        slash_status=slash,
        mode=mode,
        fake=fake.latency.quantiles(),
        guard_per_request=guard_per_request,
    )


async def run_load(
    *,
    n_tutor: int = 300,
    n_guard: int = 300,
    concurrency: int = 64,
    scale: float = 1.0,
    seed: int = 0,
    tutor_overrides: dict[str, Any] | None = None,
    fake_quantiles: tuple[float, float] = (P50_S, P95_S),
    mode: str = "v2",
    guard_calls: int = GUARD_CALLS,
) -> Report:
    """TEAM_TOKEN 환경변수가 있어야 한다(앱과 요청이 같은 값을 쓴다).

    fake_quantiles는 가짜 LLM 지연의 (p50, p95) 초다(기본은 가드 #105 실측). mode는 튜터 모드,
    guard_calls는 가드 요청당 가짜 판정 호출 수다.
    """
    rng = random.Random(seed)  # noqa: S311 - 모의 지연·순서, 암호용 아님
    fake = FakeLLM(Latency.from_quantiles(*fake_quantiles), rng, scale=scale)

    async def guard_factory(cfg: Any) -> FakeGuard:
        return FakeGuard(fake, asyncio.Semaphore(cfg.llm.gate), guard_calls)

    app = create_app(
        guard_factory,
        tutor_router=tutor.router,
        defaults={"/tutor": tutor.TUTOR_DEFAULT},
        startups=[tutor.startup],
    )
    headers = {"Authorization": f"Bearer {os.environ['TEAM_TOKEN']}"}
    # 앱 밖으로 나온 예외는 uvicorn처럼 500으로 받아 센다(실행 전체를 멈추지 않는다)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with app.router.lifespan_context(app):
        _mock_tutor_llm(app.state, fake, tutor_overrides, mode)
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
        failed, used = app.state.tutor_llm.failed, app.state.tutor_cfg.mode
    return _report(
        results,
        wall=wall,
        fake=fake,
        gate=gate,
        slash=slash,
        llm_failed=failed,
        mode=used,
        guard_per_request=guard_calls,
    )


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
    f50, f95 = report.fake
    out.write(f"가정: 튜터 모드 {report.mode}, 가짜 LLM 지연 p50 {f50:g}초·p95 {f95:g}초")
    out.write(f", 가드 판정 요청당 {report.guard_per_request}회\n")
    out.write(f"요청 {report.requests}개(튜터 {len(report.tutor_s)}·가드 {len(report.guard_s)})")
    out.write(f", 전체 {report.wall_s:.1f}초\n")
    out.write(f"튜터 p50 {t50:.2f}초 · p95 {t95:.2f}초 · 최대 {tmax:.2f}초, 가드 p95 {g95:.2f}초\n")
    out.write(f"200 아님 {report.bad_status}, 규격 밖 {report.invalid}")
    out.write(f", 시간 초과(30초 이상) {report.over_limit}, 튜터 기본값 {report.defaults}")
    out.write(f", LLM 실패 {report.llm_failed}\n")
    schemas = ", ".join(f"{name} {n}" for name, n in report.schemas) or "없음"
    out.write(
        f"공개 튜터 행 {report.covered_tutor_rows}개 응답, LLM 호출 튜터 {report.llm_calls}회"
    )
    out.write(f"({schemas}) · 가드 {report.guard_calls}회(동시 최대 {report.llm_peak}")
    out.write(f", 게이트 {report.gate}), /tutor/ {report.slash_status}\n")
    failures = []
    if report.bad_status or report.invalid:
        failures.append("형식")
    if report.over_limit:
        failures.append("시간 초과")
    if report.defaults:  # 가짜 LLM인데 기본값이면 LLM 경로가 실패한 것이다
        failures.append("기본값")
    if report.llm_failed:  # 응답 문구와 상관없이(v2 고정 문장도) LLM 경로 실패
        failures.append("LLM 실패")
    if report.llm_peak > report.gate:
        failures.append("게이트")
    if 300 <= report.slash_status < 400:
        failures.append("리다이렉트")
    if t50 > bar.p50_s:
        failures.append(f"p50 기준 넘음({bar.p50_s:g}초)")
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
    ap.add_argument("--fake-p50", type=float, default=P50_S, help="가짜 LLM 지연 p50(초)")
    ap.add_argument("--fake-p95", type=float, default=P95_S, help="가짜 LLM 지연 p95(초)")
    ap.add_argument("--max-p50", type=float, default=Bar().p50_s, help="튜터 p50 합격선(초)")
    ap.add_argument("--max-p95", type=float, default=Bar().p95_s, help="튜터 p95 합격선(초)")
    ap.add_argument("--mode", choices=("v2", "baseline"), default="v2", help="튜터 모드")
    ap.add_argument(
        "--guard-calls", type=int, default=GUARD_CALLS, help="가드 요청당 가짜 판정 호출 수"
    )
    args = ap.parse_args(argv)
    os.environ.setdefault("TEAM_TOKEN", secrets.token_hex(16))  # 프로세스 안 앱과 요청이 같이 쓴다
    report = asyncio.run(
        run_load(
            n_tutor=args.tutor,
            n_guard=args.guard,
            concurrency=args.concurrency,
            scale=args.scale,
            seed=args.seed,
            fake_quantiles=(args.fake_p50, args.fake_p95),
            mode=args.mode,
            guard_calls=args.guard_calls,
        )
    )
    return 0 if show(report, Bar(p50_s=args.max_p50, p95_s=args.max_p95)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
