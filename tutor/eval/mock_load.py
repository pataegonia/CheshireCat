"""모의 부하 시험(TU-17, #79). 저장소 루트에서 `python -m tutor.eval.mock_load`.

운영진 API를 부르지 않는다. 루트 app.py(#126)와 같은 인자로 가드 create_app에 튜터를 붙인 앱을
프로세스 안에서 띄운다. 가드 판정기만 가짜이고, create_app 호출 인자가 같은지는 테스트가 본다.
튜터·가드 요청을 섞어 동시 64로 보낸다(SPEC §3 동시 최대 64). 네트워크도 쓰지 않는다.
- 튜터 설정이 임베딩(toggles.embedding)을 켜면 돌지 않는다. 기동이 실제 임베딩 API로 KB 임베딩을
  만들고 요청마다 질의를 임베딩하게 되는데, 가짜 임베딩은 아직 없다(켤 때 더한다). 덮어쓰기로
  켜도 멈춘다(기동은 파일 설정만 읽어 임베딩 없이 재게 된다)
- 튜터 LLM은 전송만 가짜로 바꾼다. 파라미터는 서버와 같은 build_llm 값이다. 가짜 전송은 응답
  스키마 이름(REPLIES)에 맞는 최소 정상 JSON을 내므로, v2는 검색 → 주 호출 → 결정적 수정까지
  실제 흐름을 탄다. 답을 모르는 스키마는 JSON이 아닌 답이라 LLM 실패로 드러난다
- 튜터 모드는 `mode`(기본 v2, 배포 설정과 같음)로 정한다. 지금 v2는 요청당 튜터 주 호출 1회이고,
  설정(toggles.guard_classify·verify)대로 가드 classify(주 호출과 병렬)와 judge_output(주 호출
  뒤 직렬)을 가드를 거쳐 부른다(#138). 저신호 직접 판정 #98처럼 LLM 없이 끝나는 요청은 셋 다 0회
- 가드 판정기는 가짜다. 가드 설계(master-plan §4)대로 후보 답이 있으면 nano `guard_calls`회
  (기본 2: 입력·출력 판정)를 병렬로, 없으면 입력 판정 1회를 흉내 낸다. 판정 자체는 상수다.
  가드 설정의 호출당 읽기 제한(llm.timeout_s.read)과 게이트 대기를 포함한 마감(budget_s)을 넘으면
  판정을 못 끝낸 것으로 센다. 호출 하나만 실패해도 요청 전체를 실패로 보고 다른 호출은 취소하지
  않는다(가드 LLM 판정기가 들어오면 그 실패 처리와 맞춰 본다). 제한은 실제 초라서 배율을 1보다
  키우면 가드 시간 초과로 실패할 수 있다
- 가짜 가드는 튜터가 부르는 classify·judge_output도 흉내 낸다. 각각 가짜 nano 1회(같은 게이트,
  같은 호출당 읽기 제한)이고, classify는 "normal", judge_output은 통과 판정을 낸다. 가드의 실제
  classify·judge_output(S2.11)은 아직 없어 각 1회는 가정이다. 배포 설정(config/tutor.yaml)은 두
  단계를 끈다(11/14 리더 결정, S2.11 뒤 런박스 켬/끔 비교 전까지). 그래서 기본 실행은 두 단계 없이
  재고, 켠 경로는 켠 설정 파일을 TUTOR_CONFIG로 주어 잰다(요약의 가정 줄에 켬·끔이 나온다).
  시간 제한은 튜터 설정(timeout_s.classify·verify, 부른 때부터 게이트 대기 포함)이고, 넘으면
  튜터가 그 단계만 건너뛴다(state.guard_skips에 timeout으로 센다)
- 가짜 가드는 튜터 LLM과 같은 게이트를 쥔다. 이는 가드에 LLM 판정기가 붙은 구성의 가정이다(그때
  튜터가 가드 LLM의 slot()을 같이 쓴다). 지금 루트 app.py는 LLM이 없는 가드 기본 판정기라,
  배포 서버에서는 튜터가 같은 크기(llm.gate)의 자기 게이트를 따로 쓰고 가드는 LLM을 부르지 않는다
- 게이트 크기는 가드 설정 llm.gate이고 `gate`로 줄여 볼 수 있다. 경합은 게이트가 차 있어 줄을 선
  호출 수와 기다린 시간으로 잰다. 튜터 v2 요청은 classify와 주 호출을, 후보 답이 있는 가드 요청은
  두 판정을 함께 쥐므로 동시 64·게이트 64에서도 줄이 생긴다
- 가짜 지연은 로그정규 분포이고 튜터 주 호출과 가드 호출을 따로 둔다. 튜터 주 호출 기본은
  런박스 run_local 실측(main d7464a0, 동시 8, 요청 지연 p50 4.90초·p95 8.30초, `--tutor-p50`·
  `--tutor-p95`), 가드 호출(/guard 판정, 튜터의 classify·judge_output) 기본은 가드 #105 런박스
  실측(동시 64, p50 4.51초·p95 6.62초, `--guard-p50`·`--guard-p95`)이다(까닭은 P50_S 주석).
  429·5xx 재시도는 없다
- httpx ASGITransport로 앱을 바로 부른다. uvicorn·HTTP 파싱은 거치지 않는다
- 합격선은 튜터 p50 ≤6초·p95 ≤12초(TU-13·TU-17)다. 그 밖에 실패로 보는 것: 튜터 요청 없음,
  형식·시간 초과·기본값, 튜터 LLM 오류, LLM을 부르지 않은 튜터 요청이 많음, 모드에 없는 응답
  스키마, 가드 판정을 못 끝냄(가드 라우트가 예외를 기본값으로 바꿔도 센다)·가드 시간 초과, 튜터
  설정이 켠 가드 classify·judge_output을 한 번도 부르지 않음, 게이트를 거치지 않은 호출, 게이트
  크기 초과, 리다이렉트, 전체 시간 초과(WALL_LIMIT_S, 배율 1·동시 64일 때)
- 기준 조건에서 통과해야 실측 통과로 낸다. 기준은 SPEC §3 부하(튜터·가드 각 약 300, 동시 64)와
  기본 가정(가짜 지연, 가드 판정 최대 2회 병렬, 가드 기본 설정의 시간 제한, 서버 게이트)이다.
  배율이 1이 아니거나 기준보다 쉬운 조건이면 "스모크"로 내고 까닭을 적는다. 게이트에 줄을 선
  호출이 없으면 "게이트 경합 없음"을 적는다
출력은 숫자만 낸다(질문·답변 본문 없음). 가드 접속 로그(본문 없음)는 요청마다 stderr로 나온다.
"""

import argparse
import asyncio
import contextlib
import json
import math
import os
import random
import secrets
import sys
import time
from collections import Counter
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, NamedTuple, TextIO

import httpx
from guardrail.config import DEFAULT_CONFIG as GUARD_DEFAULT_CONFIG
from guardrail.config import load_config as load_guard_config
from guardrail.guard import ConstantGuard
from guardrail.schema import OutputVerdict
from guardrail.server import create_app

import tutor
from integration.contracts import Category
from tutor.eval.grader import PUBLIC_DIR, scoring
from tutor.runtime.config import load_config
from tutor.runtime.llm import Result, TutorLLM, build_llm

# 가드 호출: 가드 #105 런박스 실측(InputOut = 입력 판정, 동시 64). /guard 판정 호출과 튜터가 부르는
# classify·judge_output에 같이 쓴다. classify는 입력 판정이라 이 실측 그대로다. judge_output은 출력
# 판정이고 가드 #144 실측(동시 16)은 p50 4.1초·p95 8.7초인데, 튜터 timeout_s.verify 5초를 넘는
# 비율이 두 분포 모두 약 33%라 따로 두지 않는다(값 하나로 두어 스모크 검사도 하나로 본다). 다만
# judge_output의 꼬리(p95 8.7초)는 이 값보다 무거워, 판정 시간 초과 뒤의 꼬리 지연은 낙관이다
P50_S, P95_S = 4.51, 6.62
# 튜터 주 호출: 런박스 run_local 실측(main d7464a0, v2, 공개 200행 3회, 동시 8). 요청 지연이라
# LLM 없이 끝난 요청(주 호출 576회/600요청)도 들어 있고(낙관), 앱 처리 시간도 들어 있어 모의 앱
# 처리 시간과 겹친다(보수). 동시 64에서는 더 느릴 수 있다. baseline 모드도 같은 값을 쓴다
TUTOR_P50_S, TUTOR_P95_S = 4.90, 8.30
Z95 = 1.6448536269514722  # 표준정규 95백분위
GRADER_TIMEOUT_S = 30.0  # SPEC §3 요청당 30초
CLIENT_TIMEOUT_S = 2 * GRADER_TIMEOUT_S  # 30초를 넘는 응답도 재되, 끝없이 기다리지는 않는다
# SPEC §3: 5분을 기다린 채점은 실행 환경이 끊었고 한계는 더 짧을 수 있다. 절반은 팀 선택값이다
# (채점기 판정 몫을 남긴다)
WALL_LIMIT_S = 150.0
BASE_URL = "http://localhost"  # 프로세스 안 ASGI 호출(네트워크 없음)
GUARD_CALLS = 2  # 후보 답이 있는 가드 요청의 nano 판정 호출 수(입력·출력, 병렬). 없으면 1회
N_TUTOR = N_GUARD = 300  # SPEC §3: 한 제출은 튜터·가드 각 약 300 요청
CONCURRENCY = 64  # SPEC §3: 채점기는 최대 64개 요청을 동시에 보낸다
BAD_SKIPS = frozenset({"missing", "error", "invalid"})  # 가짜 가드가 서버와 다르게 답한 건너뜀
MIN_LLM_SHARE = 0.9  # LLM을 부른 튜터 요청의 최소 비율(고정 문장으로 바로 끝나는 경로는 적다)
# 응답 스키마 이름별 가짜 정상 답(과목 사실 없음). 없는 이름에는 JSON이 아닌 답(bad_json)을 낸다
REPLIES: dict[str, dict[str, Any]] = {
    "BaselineOut": {"answer": "자료를 바탕으로 답해요.", "action": "answer", "citations": []},
    "MainCallOut": {
        "category": "normal",
        "action": "answer",
        "sentences": [{"text": "자료를 바탕으로 답해요.", "kind": "non_claim", "evidence": []}],
    },
    "QuoteFirstMainCallOut": {
        "category": "normal",
        "action": "answer",
        "sentences": [{"kind": "non_claim", "evidence": [], "text": "자료를 바탕으로 답해요."}],
    },
    "CheckedMainCallOut": {
        "asked_in_evidence": True,
        "category": "normal",
        "action": "answer",
        "sentences": [{"text": "자료를 바탕으로 답해요.", "kind": "non_claim", "evidence": []}],
    },
}
UNKNOWN_REPLY = "모르는 스키마"  # JSON이 아니다
# 모드마다 튜터 LLM이 쓸 응답 스키마. 다른 이름이 오면(새 호출) 실패로 본다
EXPECTED_SCHEMAS: dict[str, frozenset[str]] = {
    # 근거 대조 칸 토글(#162)이면 Checked, 근거 먼저 토글(quote_first)이면 QuoteFirst
    "v2": frozenset({"MainCallOut", "CheckedMainCallOut", "QuoteFirstMainCallOut"}),
    "baseline": frozenset({"BaselineOut"}),
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


class TimedGate:
    """튜터·가드가 같이 쓰는 게이트. 줄을 선 호출 수, 기다린 시간, 역할별 진입 수를 잰다."""

    def __init__(self, size: int) -> None:
        self.size = size
        self._sem = asyncio.Semaphore(size)
        self.queued = 0  # 와 보니 게이트가 차 있던 호출 수(0이면 경합 없음)
        self.waits: list[float] = []
        self.entries: Counter[str] = Counter()

    @contextlib.asynccontextmanager
    async def slot(self, who: str) -> AsyncIterator[None]:
        started = time.perf_counter()
        if self._sem.locked():
            self.queued += 1
        async with self._sem:
            self.waits.append(time.perf_counter() - started)
            self.entries[who] += 1
            yield


def _schema_name(kwargs: dict[str, Any]) -> str:
    """요청한 응답 스키마 이름. json_object 모드는 이름이 없어 "?"다(답을 모르는 호출)."""
    fmt = kwargs.get("response_format")
    schema = fmt.get("json_schema") if isinstance(fmt, dict) else None
    return str(schema.get("name", "?")) if isinstance(schema, dict) else "?"


class FakeLLM:
    """chat.completions.create 자리. 지연만큼 기다린 뒤 응답 스키마에 맞는 가짜 정상 답을 낸다.

    latency는 가드 호출(hold: /guard 판정, 튜터의 classify·judge_output), tutor_latency는 튜터
    전송(send)의 지연 분포다(없으면 latency).
    peak는 튜터·가드 호출을 합쳐 동시에 진행 중인 호출의 최대다(게이트 크기를 넘지 않아야 한다).
    """

    def __init__(
        self,
        latency: Latency,
        rng: random.Random,
        *,
        scale: float,
        tutor_latency: Latency | None = None,
    ) -> None:
        self.latency = latency
        self.tutor_latency = tutor_latency or latency
        self.rng = rng
        self.scale = scale
        self.calls = 0  # 튜터 LLM 전송 수
        self.guard_calls = 0  # /guard 판정 호출 수(튜터의 classify·judge_output은 FakeGuard가 센다)
        self.schemas: Counter[str] = Counter()  # 튜터 전송의 응답 스키마별 수
        self.active = 0
        self.peak = 0

    async def hold(self, latency: Latency | None = None) -> None:
        """호출 하나만큼 기다린다(기본은 가드 판정 호출 지연)."""
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep((latency or self.latency).sample(self.rng) * self.scale)
        finally:
            self.active -= 1

    async def send(self, kwargs: dict[str, Any]) -> Any:
        self.calls += 1
        name = _schema_name(kwargs)
        self.schemas[name] += 1
        await self.hold(self.tutor_latency)
        reply = REPLIES.get(name)
        content = UNKNOWN_REPLY if reply is None else json.dumps(reply, ensure_ascii=False)
        choice = SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")
        return SimpleNamespace(choices=[choice], usage=SimpleNamespace(completion_tokens=40))


class FakeGuard:
    """가드 판정기 자리. 요청마다 nano 호출을 공유 게이트를 쥔 채 병렬로 흉내 낸다(`calls_for`).

    판정 자체는 상수(ConstantGuard)다. judged는 판정을 끝낸 요청 수다. 가드 라우트는 판정기 예외와
    예산 초과를 200 기본값으로 바꾸므로, 응답 형식이 아니라 이 수로 가드 실패를 센다.
    가드 LLM처럼 호출마다 read_s(보낸 뒤 기다리는 상한), 요청마다 budget_s(게이트 대기 포함 마감)를
    넘으면 판정을 못 끝낸 것으로 보고 timeouts를 올린 뒤 예외를 낸다.
    튜터가 부르는 classify·judge_output(tutor.judge.guard_merge.GuardLike)도 각각 가짜 호출 1회로
    흉내 내고 부른 수를 stage_calls에 센다. 시간 초과는 튜터가 state.guard_skips에 센다.
    """

    def __init__(
        self, fake: FakeLLM, gate: TimedGate, calls: int, *, read_s: float, budget_s: float
    ) -> None:
        self.fake = fake
        self.gate = gate  # 튜터 LLM도 이 게이트를 쓴다(가드 LLM 판정기가 붙은 구성의 가정)
        self.calls = calls
        self.read_s = read_s  # 가드 llm.timeout_s.read
        self.budget_s = budget_s  # 가드 budget_s
        self.inner = ConstantGuard()
        self.judged = 0
        self.timeouts = 0  # 읽기 제한·마감을 넘어 판정을 못 끝낸 요청 수
        self.stage_calls: Counter[str] = Counter()  # 튜터가 부른 classify·judge_output 수

    async def judge(self, req: Any) -> Any:
        # 가드 설계(master-plan §4): 입력·출력 판정을 병렬로, 출력 판정은 후보 답이 있을 때만.
        # 가드 has_candidate는 키가 있는지로 보지만, 빈 답은 출력 판정을 부를 일이 없어
        # 내용으로 본다(모의 부하는 빈 답이면 키를 보내지 않아 공개 세트에서는 둘이 같다)
        n = calls_for(self.calls, bool(getattr(req, "candidate_answer", None)))
        try:
            async with asyncio.timeout(self.budget_s):
                done = await asyncio.gather(*(self._call() for _ in range(n)))
            if not all(done):
                raise TimeoutError("가짜 가드 판정 시간 초과")  # 라우트가 200 기본값으로 바꾼다
        except (TimeoutError, asyncio.CancelledError):  # 라우트 안전망이 먼저 끊은 것도 센다
            self.timeouts += 1
            raise
        judgement = await self.inner.judge(req)
        self.judged += 1
        return judgement

    async def _call(self) -> bool:
        """판정 호출 하나. 읽기 제한 안에 끝나면 True."""
        async with self.gate.slot("guard"):
            self.fake.guard_calls += 1
            try:
                async with asyncio.timeout(self.read_s):
                    await self.fake.hold()
            except TimeoutError:
                return False
        return True

    async def classify(self, messages: list[dict[str, str]]) -> Category:
        """튜터 classify(주 호출과 병렬): 가드 입력 판정 1회. 덮어쓰기 없는 "normal"을 낸다."""
        await self._stage_call("classify")
        return "normal"

    async def judge_output(
        self,
        messages: list[dict[str, str]],
        candidate_answer: str,
        category: Category | None = None,
    ) -> OutputVerdict:
        """튜터 judge_output(주 호출 뒤 직렬): 가드 출력 판정 1회. 통과 판정을 낸다."""
        await self._stage_call("judge_output")
        return OutputVerdict(grounded=True, unsupported=(), safe=True)

    async def _stage_call(self, stage: str) -> None:
        """/guard 판정 호출처럼 공유 게이트를 쥐고 호출당 read_s 안에 끝낸다. 넘으면 TimeoutError.

        부른 때 센다(게이트에서 기다리다 튜터 시간 제한으로 취소돼도 부른 것이다)."""
        self.stage_calls[stage] += 1
        # 게이트 진입은 "stage"로 따로 센다(/guard 판정 호출의 게이트 우회 검사를 흐리지 않게)
        async with self.gate.slot("stage"), asyncio.timeout(self.read_s):
            await self.fake.hold()


def base_guard_limits() -> tuple[float, float]:
    """기준 가드 시간 제한 (읽기 제한, 마감) 초. GUARD_CONFIG와 상관없이 가드 기본 설정을 읽는다."""
    cfg = load_guard_config(GUARD_DEFAULT_CONFIG)
    return cfg.llm.timeout_s.read, cfg.budget_s


def calls_for(per_request: int, has_candidate: bool) -> int:
    """가드 요청 하나의 판정 호출 수: 후보 답이 있으면 per_request, 없으면 입력 판정 1회까지."""
    return per_request if has_candidate else min(per_request, 1)


class CountingLLM(TutorLLM):
    """complete 호출(LLM을 부른 요청)·오류로 끝난 호출·다시 보낸 호출 수를 센다."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.completes = 0
        self.failed = 0
        self.retried = 0

    async def complete(self, *args: Any, **kwargs: Any) -> Result:
        self.completes += 1
        result = await super().complete(*args, **kwargs)
        if result.error is not None:
            self.failed += 1
        if result.attempts > 1:
            self.retried += 1
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
    llm_calls: int  # 튜터 LLM 전송 수(다시 보낸 것 포함)
    llm_requests: int  # 튜터 LLM을 부른 횟수(complete). v2는 요청당 최대 1회(직접 판정은 0회)
    retried: int  # 다시 보낸 튜터 LLM 호출 수
    guard_calls: int  # 가드 판정 호출 수
    guard_expected: int  # 가드 요청별 calls_for의 합(후보 답 있으면 요청당 판정 수, 없으면 1)
    guard_judged: int  # 판정을 끝낸 가드 요청 수
    guard_timeouts: int  # 가드 읽기 제한·마감을 넘어 판정을 못 끝낸 가드 요청 수
    guard_limits: tuple[float, float]  # 가짜 가드의 (읽기 제한, 마감) 초. 가드 설정에서 읽는다
    # 튜터 v2 설정이 가드 classify(toggles.guard_classify가 off 아님)·judge_output(verify: guard)을
    # 켰는가. baseline 모드는 둘 다 부르지 않는다
    classify_on: bool
    judge_output_on: bool
    classify_calls: int  # 튜터가 부른 가드 classify 수
    judge_output_calls: int  # 튜터가 부른 가드 judge_output 수
    # 튜터 가드 단계 건너뜀(state.guard_skips): ("단계 사유", 수). 사유는 timeout·no_time 등
    guard_skips: tuple[tuple[str, int], ...]
    schemas: tuple[tuple[str, int], ...]  # 튜터 전송의 응답 스키마별 수
    gate_entries: tuple[tuple[str, int], ...]  # 게이트에 들어간 수(역할별)
    gate_queued: int  # 게이트가 차 있어 줄을 선 호출 수(0이면 경합 없음)
    gate_wait_p95: float  # 게이트에 들어가기까지 기다린 시간(초)
    gate_wait_max: float
    llm_peak: int  # 튜터·가드 호출을 합쳐 동시에 진행 중인 최대
    gate: int  # 게이트 크기
    server_gate: int  # 가드 설정 llm.gate(서버의 게이트 크기)
    slash_status: int  # POST /tutor/ 상태 코드(3xx면 리다이렉트)
    mode: str  # 튜터 모드
    fake: tuple[float, float]  # 가짜 가드 호출 지연 (p50, p95) 초(배율 전, classify·judge_output도)
    tutor_fake: tuple[float, float]  # 가짜 튜터 주 호출 지연 (p50, p95) 초(배율 전)
    scale: float  # 가짜 지연 배율. 1이 아니면 지연은 실측이 아니다
    guard_per_request: int  # 후보 답이 있는 가드 요청의 판정 호출 수(가정, 없으면 1회)
    concurrency: int  # 동시에 보낸 요청 수 상한


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
    shared: TimedGate = state.guard.gate
    state.tutor_cfg = cfg
    state.tutor_llm = CountingLLM(
        fake.send, lambda: shared.slot("tutor"), model=built.model, base=built
    )


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
    gate: TimedGate,
    llm: Any,  # CountingLLM(completes·failed·retried)
    guard_judged: int,
    guard_timeouts: int,
    guard_limits: tuple[float, float],
    stages_on: tuple[bool, bool],  # (classify_on, judge_output_on)
    stage_calls: Mapping[str, int],  # FakeGuard.stage_calls
    guard_skips: Mapping[tuple[str, str], int],  # state.guard_skips
    slash: int,
    mode: str,
    guard_per_request: int,
    concurrency: int,
    server_gate: int,
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
        llm_failed=llm.failed,
        covered_tutor_rows=len(covered),
        llm_calls=fake.calls,
        llm_requests=llm.completes,
        retried=llm.retried,
        guard_calls=fake.guard_calls,
        guard_expected=sum(
            calls_for(guard_per_request, r.has_candidate) for r in results if r.endpoint != "tutor"
        ),
        guard_judged=guard_judged,
        guard_timeouts=guard_timeouts,
        guard_limits=guard_limits,
        classify_on=stages_on[0],
        judge_output_on=stages_on[1],
        classify_calls=stage_calls.get("classify", 0),
        judge_output_calls=stage_calls.get("judge_output", 0),
        guard_skips=tuple(sorted((f"{st} {why}", n) for (st, why), n in guard_skips.items())),
        schemas=tuple(sorted(fake.schemas.items())),
        gate_entries=tuple(sorted(gate.entries.items())),
        gate_queued=gate.queued,
        gate_wait_p95=_pct(gate.waits, 0.95),
        gate_wait_max=max(gate.waits, default=0.0),
        llm_peak=fake.peak,
        gate=gate.size,
        server_gate=server_gate,
        slash_status=slash,
        mode=mode,
        fake=fake.latency.quantiles(),
        tutor_fake=fake.tutor_latency.quantiles(),
        scale=fake.scale,
        guard_per_request=guard_per_request,
        concurrency=concurrency,
    )


async def run_load(
    *,
    n_tutor: int = N_TUTOR,
    n_guard: int = N_GUARD,
    concurrency: int = CONCURRENCY,
    scale: float = 1.0,
    seed: int = 0,
    tutor_overrides: dict[str, Any] | None = None,
    fake_quantiles: tuple[float, float] = (P50_S, P95_S),
    tutor_quantiles: tuple[float, float] = (TUTOR_P50_S, TUTOR_P95_S),
    mode: str = "v2",
    guard_calls: int = GUARD_CALLS,
    gate: int | None = None,
) -> Report:
    """TEAM_TOKEN 환경변수가 있어야 한다(앱과 요청이 같은 값을 쓴다).

    fake_quantiles는 가짜 가드 호출(/guard 판정, 튜터의 classify·judge_output), tutor_quantiles는
    가짜 튜터 주 호출 지연의 (p50, p95) 초다(기본은 각각 가드 #105 실측, 튜터 run_local 실측).
    mode는 튜터 모드,
    guard_calls는 후보 답이 있는 가드 요청의 가짜 판정 호출 수(없으면 1회), gate는 게이트 크기
    (없으면 가드 설정 llm.gate)다.
    """
    if load_config().toggles.embedding:  # 기동은 덮어쓰기 없이 파일 설정(TUTOR_CONFIG)을 읽는다
        raise RuntimeError("임베딩이 켜진 튜터 설정이라 운영진 임베딩 API를 부르게 된다")
    if load_config(overrides=tutor_overrides).toggles.embedding:
        raise RuntimeError("덮어쓰기로 켠 임베딩은 기동이 만들지 않아 임베딩 없이 재게 된다")
    rng = random.Random(seed)  # noqa: S311 - 모의 지연·순서, 암호용 아님
    fake = FakeLLM(
        Latency.from_quantiles(*fake_quantiles),
        rng,
        scale=scale,
        tutor_latency=Latency.from_quantiles(*tutor_quantiles),
    )

    async def guard_factory(cfg: Any) -> FakeGuard:
        size = gate if gate is not None else cfg.llm.gate
        limits = {"read_s": cfg.llm.timeout_s.read, "budget_s": cfg.budget_s}
        return FakeGuard(fake, TimedGate(size), guard_calls, **limits)

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
        guard, llm = app.state.guard, app.state.tutor_llm
        used, server_gate = app.state.tutor_cfg.mode, app.state.cfg.llm.gate
        toggles, skips = app.state.tutor_cfg.toggles, app.state.guard_skips
    v2 = used == "v2"  # baseline은 가드 단계를 부르지 않는다
    stages_on = (v2 and toggles.guard_classify != "off", v2 and toggles.verify == "guard")
    return _report(
        results,
        wall=wall,
        fake=fake,
        gate=guard.gate,
        llm=llm,
        guard_judged=guard.judged,
        guard_timeouts=guard.timeouts,
        guard_limits=(guard.read_s, guard.budget_s),
        stages_on=stages_on,
        stage_calls=guard.stage_calls,
        guard_skips=skips,
        slash=slash,
        mode=used,
        guard_per_request=guard_calls,
        concurrency=concurrency,
        server_gate=server_gate,
    )


def _pct(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]


def _failures(report: Report, bar: Bar, t50: float, t95: float) -> list[str]:
    """수용 기준에서 어긋난 것들(이름만)."""
    n_tutor, n_guard = len(report.tutor_s), len(report.guard_s)
    entries = dict(report.gate_entries)
    checks = [
        ("튜터 요청 없음", not n_tutor),
        ("형식", bool(report.bad_status or report.invalid)),
        ("시간 초과", bool(report.over_limit)),
        ("기본값", bool(report.defaults)),  # 가짜 LLM인데 기본값이면 LLM 경로가 실패한 것이다
        ("LLM 실패", bool(report.llm_failed)),  # 응답 문구와 상관없이(v2 고정 문장도)
        # v2 고정 문장은 라우터 기본값과 달라 본문으로는 못 센다. LLM을 부른 요청 수로 본다
        ("LLM 경로 적음", bool(n_tutor) and report.llm_requests < MIN_LLM_SHARE * n_tutor),
        ("스키마", bool(set(dict(report.schemas)) - EXPECTED_SCHEMAS.get(report.mode, set()))),
        (
            "가드 판정",  # 판정기 예외·예산 초과는 라우트가 200 기본값으로 바꾼다
            report.guard_judged != n_guard or report.guard_calls != report.guard_expected,
        ),
        ("가드 시간 초과", bool(report.guard_timeouts)),
        # 설정이 켠 가드 단계를 한 번도 부르지 않았으면 서버(가드 S2.11 뒤)와 다른 경로를 잰 것이다
        # (#159 리뷰: 가짜 가드에 classify·judge_output이 없어 주 호출만 재던 일). classify는 주
        # 호출 앞에서 시작하고, judge_output은 주 호출이 성공한 뒤에만 부른다
        (
            "가드 classify 안 부름",
            report.classify_on and report.llm_requests > 0 and not report.classify_calls,
        ),
        (
            "가드 judge_output 안 부름",
            report.judge_output_on
            and report.llm_requests > report.llm_failed
            and not report.judge_output_calls,
        ),
        # 튜터가 가드 단계를 없음·오류·형식 밖으로 건너뛰었으면 가짜 가드가 서버와 다르게
        # 답한 것이다.
        # 시간 초과·남은 시간 없음(timeout·no_time)은 튜터 설계대로라 실패로 보지 않는다
        (
            "가드 단계 건너뜀(missing·error·invalid)",
            any(name.split()[-1] in BAD_SKIPS for name, _n in report.guard_skips),
        ),
        # 보낸 호출은 모두 게이트를 지난다. 들어간 뒤 시간이 모자라 보내지 않고 나오면 진입이
        # 더 많아지는데, 그 호출은 오류로 끝나 "LLM 실패"로 따로 걸린다(일부 우회가 가려져도
        # 통과하지 못한다)
        (
            "게이트 우회",
            entries.get("tutor", 0) < report.llm_calls
            or entries.get("guard", 0) < report.guard_calls,
        ),
        ("게이트", report.llm_peak > report.gate),
        ("리다이렉트", 300 <= report.slash_status < 400),
        (f"p50 기준 넘음({bar.p50_s:g}초)", t50 > bar.p50_s),
        (f"p95 기준 넘음({bar.p95_s:g}초)", t95 > bar.p95_s),
        # 배율·낮은 동시는 전체 시간을 부풀리므로 실제 시간 부하(스모크 표시도 따로 붙음)에서만 본다
        (
            f"전체 {WALL_LIMIT_S:g}초 넘음",
            report.scale == 1
            and report.concurrency >= CONCURRENCY
            and report.wall_s > WALL_LIMIT_S,
        ),
    ]
    return [name for name, failed in checks if failed]


def _shorter(value: float, base: float) -> bool:
    """value가 base보다 작다(분포 모수를 거쳐 다시 계산한 값의 반올림 오차는 같은 값으로 본다)."""
    return value < base and not math.isclose(value, base)


def _smoke_reasons(report: Report) -> list[str]:
    """통과해도 실측 통과로 볼 수 없는 까닭. 배율이 1이 아니거나 기준보다 쉬운 조건이다.

    기준은 SPEC §3 부하(튜터·가드 각 약 300, 동시 64)와 기본 가정(튜터 주 호출·가드 호출 실측 지연,
    가드 판정 요청당 2회, 서버 게이트)이다. 가드 호출 지연은 튜터의 classify·judge_output에도 쓰므로
    "가짜 가드 지연" 검사 하나로 함께 본다. 더 어려운 조건(지연이 길거나, 게이트가 작거나,
    요청·동시·판정 호출이 많음)은 그대로 실측으로 둔다.
    """
    n_tutor, n_guard = len(report.tutor_s), len(report.guard_s)
    f50, f95 = report.fake
    t50, t95 = report.tutor_fake
    per_request = report.guard_per_request
    (read_s, budget_s), (base_read, base_budget) = report.guard_limits, base_guard_limits()
    checks = [
        (f"배율 {report.scale:g}", report.scale != 1),
        ("가짜 가드 지연이 기본보다 짧음", _shorter(f50, P50_S) or _shorter(f95, P95_S)),
        (
            "가짜 튜터 지연이 기본보다 짧음",
            _shorter(t50, TUTOR_P50_S) or _shorter(t95, TUTOR_P95_S),
        ),
        (f"가드 판정 최대 {per_request}회 < {GUARD_CALLS}회", per_request < GUARD_CALLS),
        ("가드 시간 제한이 기본보다 김", read_s > base_read or budget_s > base_budget),
        (f"게이트 {report.gate} > 서버 {report.server_gate}", report.gate > report.server_gate),
        (f"동시 {report.concurrency} < {CONCURRENCY}", report.concurrency < CONCURRENCY),
        (f"튜터 요청 {n_tutor} < {N_TUTOR}", n_tutor < N_TUTOR),
        (f"가드 요청 {n_guard} < {N_GUARD}", n_guard < N_GUARD),
    ]
    return [name for name, hit in checks if hit]


def show(report: Report, bar: Bar, out: TextIO | None = None) -> bool:
    """요약을 내고 수용 기준을 통과했는지 돌려준다."""
    out = out or sys.stdout  # 부를 때의 stdout(기본 인자로 묶지 않는다)
    n_tutor, n_guard = len(report.tutor_s), len(report.guard_s)
    t50, t95 = _pct(report.tutor_s, 0.5), _pct(report.tutor_s, 0.95)
    tmax, g95 = max(report.tutor_s, default=0.0), _pct(report.guard_s, 0.95)
    f50, f95 = report.fake
    s50, s95 = report.tutor_fake
    out.write(f"가정: 튜터 모드 {report.mode}, 가짜 지연 튜터 주 호출 p50 {s50:g}초·p95 {s95:g}초")
    out.write(f"·가드 판정 p50 {f50:g}초·p95 {f95:g}초")
    read_s, budget_s = report.guard_limits
    out.write(f"(배율 {report.scale:g}), 가드 판정 최대 {report.guard_per_request}회 병렬")
    out.write(f"(읽기 제한 {read_s:g}초·마감 {budget_s:g}초)")
    out.write(f", 게이트 {report.gate}, 동시 {report.concurrency}")
    onoff = {True: "켬", False: "끔"}
    out.write(f", 튜터 가드 단계 classify {onoff[report.classify_on]}")
    out.write(f"·judge_output {onoff[report.judge_output_on]}\n")
    out.write(
        f"요청 {report.requests}개(튜터 {n_tutor}·가드 {n_guard}), 전체 {report.wall_s:.1f}초\n"
    )
    out.write(f"튜터 p50 {t50:.2f}초 · p95 {t95:.2f}초 · 최대 {tmax:.2f}초, 가드 p95 {g95:.2f}초\n")
    out.write(f"200 아님 {report.bad_status}, 규격 밖 {report.invalid}")
    out.write(f", 시간 초과(30초 이상) {report.over_limit}, 튜터 기본값 {report.defaults}")
    out.write(f", LLM 실패 {report.llm_failed}\n")
    schemas = ", ".join(f"{name} {n}" for name, n in report.schemas) or "없음"
    out.write(
        f"공개 튜터 행 {report.covered_tutor_rows}개 응답, LLM 호출 튜터 {report.llm_calls}회"
    )
    out.write(f"({schemas}, 재시도 {report.retried}) · 가드 {report.guard_calls}회\n")
    out.write(f"LLM을 부른 튜터 요청 {report.llm_requests}/{n_tutor}")
    out.write(f", 판정을 끝낸 가드 요청 {report.guard_judged}/{n_guard}")
    out.write(f"(시간 초과 {report.guard_timeouts})\n")
    skipped = ", ".join(f"{name} {n}" for name, n in report.guard_skips) or "없음"
    out.write(f"튜터의 가드 호출 classify {report.classify_calls}회")
    out.write(f" · judge_output {report.judge_output_calls}회(건너뜀 {skipped})\n")
    out.write(f"게이트 줄 선 호출 {report.gate_queued}회 · 대기 p95 {report.gate_wait_p95:.2f}초")
    out.write(f" · 최대 {report.gate_wait_max:.2f}초")
    out.write(
        f", 동시 최대 {report.llm_peak}(게이트 {report.gate}), /tutor/ {report.slash_status}\n"
    )
    failures = _failures(report, bar, t50, t95)
    smoke = _smoke_reasons(report)
    if failures:
        out.write(f"실패: {', '.join(failures)}\n")
    elif smoke:
        out.write(f"통과(스모크: {', '.join(smoke)}, 실측 아님)\n")
    else:
        quiet = "" if report.gate_queued else ", 게이트 경합 없음"
        out.write(f"통과(튜터 p50 ≤{bar.p50_s:g}초·p95 ≤{bar.p95_s:g}초{quiet})\n")
    return not failures


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="모의 부하 시험(TU-17). 운영진 API를 부르지 않는다")
    ap.add_argument("--tutor", type=int, default=N_TUTOR)
    ap.add_argument("--guard", type=int, default=N_GUARD)
    ap.add_argument("--concurrency", type=int, default=CONCURRENCY)
    ap.add_argument("--scale", type=float, default=1.0, help="가짜 지연 배율(1은 실측 그대로)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument(
        "--guard-p50",
        type=float,
        default=P50_S,
        help="가짜 가드 호출 지연 p50(초, /guard 판정·튜터의 classify·judge_output)",
    )
    ap.add_argument(
        "--guard-p95",
        type=float,
        default=P95_S,
        help="가짜 가드 호출 지연 p95(초, /guard 판정·튜터의 classify·judge_output)",
    )
    ap.add_argument(
        "--tutor-p50", type=float, default=TUTOR_P50_S, help="가짜 튜터 주 호출 p50(초)"
    )
    ap.add_argument(
        "--tutor-p95", type=float, default=TUTOR_P95_S, help="가짜 튜터 주 호출 p95(초)"
    )
    ap.add_argument("--max-p50", type=float, default=Bar().p50_s, help="튜터 p50 합격선(초)")
    ap.add_argument("--max-p95", type=float, default=Bar().p95_s, help="튜터 p95 합격선(초)")
    ap.add_argument("--mode", choices=("v2", "baseline"), default="v2", help="튜터 모드")
    ap.add_argument(
        "--guard-calls",
        type=int,
        default=GUARD_CALLS,
        help="후보 답이 있는 가드 요청의 가짜 판정 호출 수(병렬, 없으면 1회)",
    )
    ap.add_argument("--gate", type=int, help="게이트 크기(기본은 가드 설정 llm.gate)")
    args = ap.parse_args(argv)
    if args.gate is not None and args.gate < 1:  # Semaphore(0)은 끝없이 기다린다
        ap.error("--gate는 1 이상이어야 한다")
    if args.concurrency < 1:  # 같은 이유(요청 시간 제한은 이 세마포어 안쪽에 있다)
        ap.error("--concurrency는 1 이상이어야 한다")
    if args.guard_calls < 0:
        ap.error("--guard-calls는 0 이상이어야 한다")
    if args.scale <= 0 or args.guard_p50 <= 0 or args.guard_p95 < args.guard_p50:
        ap.error("--scale·--guard-p50은 0보다 크고 --guard-p95는 --guard-p50 이상이어야 한다")
    if args.tutor_p50 <= 0 or args.tutor_p95 < args.tutor_p50:
        ap.error("--tutor-p50은 0보다 크고 --tutor-p95는 --tutor-p50 이상이어야 한다")
    os.environ.setdefault("TEAM_TOKEN", secrets.token_hex(16))  # 프로세스 안 앱과 요청이 같이 쓴다
    report = asyncio.run(
        run_load(
            n_tutor=args.tutor,
            n_guard=args.guard,
            concurrency=args.concurrency,
            scale=args.scale,
            seed=args.seed,
            fake_quantiles=(args.guard_p50, args.guard_p95),
            tutor_quantiles=(args.tutor_p50, args.tutor_p95),
            mode=args.mode,
            guard_calls=args.guard_calls,
            gate=args.gate,
        )
    )
    return 0 if show(report, Bar(p50_s=args.max_p50, p95_s=args.max_p95)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
