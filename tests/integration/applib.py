"""I1.4 조립 테스트 공용: 가짜 LLM 전송, 두 경로의 규격 검사, 요청 내용이 남았는지 찾는 탐색."""

import collections
import functools
import hashlib
import importlib.util
import io
import itertools
import json
import re
import sys
import types
from collections.abc import Callable, Generator, Iterator, Mapping
from pathlib import Path
from typing import Any, Literal, NamedTuple, get_args, get_origin

import guardrail.judges.schemas as guard_schemas
import jsonschema
import openai
import pytest
import yaml
from guardrail.judges.schemas import strict_schema
from guardrail.schema import Category
from pydantic import BaseModel, ValidationError

from integration.contracts import ACTIONS

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "integration-test-token"  # noqa: S105  테스트용 가짜 값
AUTH = {"Authorization": f"Bearer {TOKEN}"}

TUTOR_SCHEMA = json.loads((ROOT / "tutor/contract/tutor.schema.json").read_text("utf-8"))
_GUARD_CONTRACT = json.loads(
    (ROOT / "guardrail/docs/contract/guard.schema.json").read_text("utf-8")
)
GUARD_RESPONSE_SCHEMA = {"$ref": "#/$defs/response", "$defs": _GUARD_CONTRACT["$defs"]}

ANSWER_WORD = "가짜답변표식"  # 가짜 LLM이 답변에 넣는 단어(로그에 나오면 답변 본문이 샌 것)
# 가짜 가드 LLM이 판정 글(evidence · reason)에 넣는 단어(응답 · 로그에 나오면 판정 글이 샌 것)
GUARD_WORD = "가짜판정표식"


def _load_scoring():
    spec = importlib.util.spec_from_file_location(
        "_grader_scoring_app", ROOT / "competition/grader/scoring.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scoring = _load_scoring()


def assert_tutor_contract(obj: Any) -> None:
    jsonschema.validate(obj, TUTOR_SCHEMA)
    assert scoring.parse_tutor(obj) is not None


def assert_guard_contract(obj: Any, *, has_candidate: bool) -> None:
    jsonschema.validate(obj, GUARD_RESPONSE_SCHEMA)
    assert scoring.parse_guard(obj, has_candidate) is not None
    assert ("output" in obj) == has_candidate


def as_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode()


def user(content: str) -> dict[str, Any]:
    return {
        "spec_version": "1.0",
        "session_id": "s_x",
        "messages": [{"role": "user", "content": content}],
    }


# 튜터의 가드 단계(classify 합치기 · judge_output 검증)를 켠 값. 배포 설정(config/tutor.yaml)은
# 둘 다 끈다(11/14 리더 결정, 가드 #170(S2.11) 뒤 런박스 켬/끔 비교 전까지)
TUTOR_GUARD_STAGES_ON = {"guard_classify": "parallel", "verify": "guard"}


# 튜터 동결 설정(11/18, 튜터 #220 · #225 · #232 · #236)과 영어 장치(11/19, #242)가 켜는 토글을
# 끈 값. 통합 테스트는 이 토글들을 끈 기준 설정에서 돈다(튜터 테스트 tests/tutor/conftest.py와
# 같은 방식). 저장소 설정 그대로의 앱은 test_freeze_config.py가 본다. 저장소 설정에 아직 없는
# 키는 건드리지 않는다(동결 설정 전 main)
FREEZE_TOGGLES_OFF = {
    "delegation_override": False,
    "fallback_quote": False,
    "intent_override": "off",
    "quote_append": False,  # 튜터 #225: 최종 answer에 자료 문장 덧붙이기
    "quote_from_top1": False,  # 튜터: 덧붙일 자료 문장을 cite_top1이 더한 1위 섹션에서 고르기
    "top1_dialog_query": False,  # 튜터 #236: 인용에 더할 검색 1위를 대화 질의로 고르기
    "oom_gate": "off",  # 튜터 #232: 자료 밖 확인 E(OomOut, 주 호출과 동시에 nano 1회)
    # 튜터 oom_schedule: E를 보내는 때(동결 설정 after_intent_fresh, 기준은 요청 시작 때)
    "oom_schedule": "with_request",
    # 튜터 #242 english_embedding: 영어 발화는 임베딩 순위만 + 영어에도 E. E를 끈 기준에서는
    # 켤 수 없다(튜터 설정 로드에서 거부)
    "english_embedding": False,
}


def write_tutor_config(path: Path, *, mode: str | None = None, **toggles: str) -> Path:
    """config/tutor.yaml의 기준 설정 사본을 path에 쓴다(주석은 빠진다).

    FREEZE_TOGGLES_OFF 가운데 저장소 설정에 실제로 있는 키만 끄고, 그 위에 `toggles`를 덮어 쓴다.
    mode를 주면 최상위 mode도 바꾼다.
    """
    raw = yaml.safe_load((ROOT / "config" / "tutor.yaml").read_text(encoding="utf-8"))
    off = {k: v for k, v in FREEZE_TOGGLES_OFF.items() if k in raw["toggles"]}
    raw["toggles"] = {**raw["toggles"], **off, **toggles}
    if mode is not None:
        raw["mode"] = mode
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")  # "off"는 따옴표로
    return path


# 요청 하나에 튜터가 **자기 전송 함수로** LLM을 부르는 횟수의 상한(모드별). 튜터가 자기 호출을
# 늘리면 여기 한 곳과 FakeSend의 응답(스키마 이름별 분기)을 고친다. 튜터가 가드에 맡기는 판정
# (classify · judge_output, #88)은 튜터 전송 함수를 지나지 않는다: 아래 가드 쪽 상한에서 센다.
# 기준 설정 기준이며, 저장소 설정 그대로의 호출 수는 test_freeze_config.py가 본다.
MAX_LLM_CALLS_PER_REQUEST = {"v2": 1, "baseline": 1}

# 요청 하나에 가드 LLM(`guardrail.llm.LLM`)이 nano를 부르는 횟수의 상한(경로별). probe는 세지
# 않는다(기동 때). 요청당 전체 nano 호출 상한은 위 표와 이 표의 합이다.
# 이 표는 진짜 판정기의 상한이다. 상한은 `guard_call_budget`으로만 읽는다.
# - 기본 판정기는 가드 `default_factory`다: OPENAI_API_KEY가 없으면 상수 판정기라, 키를 지우는
#   `app_env`로 띄우는 기존 테스트의 상한은 0이다(`real_guard=False`). 진짜 판정기 경로는
#   test_real_guard.py가 `real_guard=True`로 본다.
# - `/guard` 3: 입력 판정 1 + 탈옥 2차 판정 1(1차가 jailbreak_injection일 때만, 가드 #183
#   `toggles.jb_check`) + 출력 판정 1(후보 답변이 있을 때).
# - `/tutor` 3: 가드가 튜터용 메서드를 올린 뒤(이슈 #119) classify 1(탈옥이면 2차로 +1) +
#   judge_output 1. 튜터는 judge_output에 범주를 넘긴다(넘기지 않으면 가드가 classify를 한 번 더
#   부른다, 가드 interface.md의 judge_output 설명).
# - 가짜 전송은 실패를 내지 않아 재시도 몫은 넣지 않는다. 키 없이 뜬 상수 판정기 앱은 0이다.
MAX_GUARD_LLM_CALLS_PER_REQUEST = {"/tutor": 3, "/guard": 3}
GUARD_BUDGET_HINT = (
    "가드 nano 호출이 상한을 넘었다. 판정기를 바꿨으면 "
    "applib.MAX_GUARD_LLM_CALLS_PER_REQUEST · guard_call_budget을 고친다. "
    "가드 PR에서 이 실패가 나면 통합 담당(pyr27)에게 알린다(tests/는 통합 트랙이 고친다)"
)


def guard_call_budget(*, tutor: int = 0, guard: int = 0, real_guard: bool = False) -> int:
    """`/tutor` 요청 tutor개와 `/guard` 요청 guard개에 허용되는 가드 nano 호출 수.

    키 없이 띄운 앱(상수 판정기, `real_guard=False`)은 nano를 부르지 않으므로 0이다.
    """
    if not real_guard:
        return 0
    limits = MAX_GUARD_LLM_CALLS_PER_REQUEST
    return tutor * limits["/tutor"] + guard * limits["/guard"]


def guard_calls_per_request(path: str, *, real_guard: bool = False) -> int:
    """`path` 요청 하나에 허용되는 가드 nano 호출 수(`guard_call_budget`을 거친다)."""
    counts = {"/tutor": {"tutor": 1}, "/guard": {"guard": 1}}[path]
    return guard_call_budget(**counts, real_guard=real_guard)


CATEGORIES: tuple[str, ...] = get_args(Category)
LONG_TAIL = " " + "길게 이어지는 문장이에요. " * 400  # 5,600자: 답변 상한 4,000자를 넘긴다
MISSING_SIDS = ("PY-99-99", "FIN-99-99")  # 모양은 맞지만 없는 섹션
_SID_IN_PROMPT = re.compile(r"\[((?:PY|PR|FIN)-\d{2}-\d{2})\]")  # baseline 프롬프트의 섹션 표기
_SECTION_IN_PROMPT = re.compile(r'<섹션 id="([^"]+)">\n(.*?)\n</섹션>', re.S)  # v2 프롬프트


class EvidenceLog(NamedTuple):
    """가짜 LLM이 호출 하나에서 낸 근거의 기록. 섹션 ID와 참 · 거짓만 담는다(요청 내용 없음)."""

    first_sid: str | None  # 프롬프트의 첫 섹션(검색 1위). 섹션이 없으면 None
    items: tuple[tuple[str, bool], ...]  # (sid, 인용 문구가 원문에 없는가)

    def wrong_only_sids(self) -> set[str]:
        """틀린 문구로만 인용된 섹션. 맞는 문구가 하나라도 같이 있는 섹션은 뺀다."""
        right = {sid for sid, wrong in self.items if not wrong}
        return {sid for sid, wrong in self.items if wrong} - right


NO_EVIDENCE = EvidenceLog(first_sid=None, items=())


class UnknownSchema(AssertionError):
    """튜터가 가짜 전송이 모르는 스키마로 LLM을 불렀다(새 호출이 붙었다). FakeSend를 고친다."""


class IntentFailure(RuntimeError):
    """`FakeSend.intent_fails`일 때 T 의도 판정(IntentOut) 호출이 내는 가짜 실패."""


INTENT_STUDY = "학습 질문"  # T 의도 판정의 덮지 않는 이름표(tutor.judge.intent)
OOM_GATE = "OomOut"  # 튜터 자료 밖 확인 E의 응답 스키마 이름(tutor.judge.oom_gate)


def _schema_name(kwargs: dict[str, Any], system: str) -> str:
    """요청한 응답 스키마 이름. json_schema 방식이면 이름으로, json_object 방식이면 system에
    붙은 스키마의 필드로 알아본다."""
    spec = kwargs.get("response_format", {}).get("json_schema")
    if isinstance(spec, dict):
        return str(spec.get("name", ""))
    if '"sentences"' in system and '"category"' in system:
        return "MainCallOut"
    if '"label"' in system and '"self_info"' in system:
        return "IntentOut"
    if '"asked"' in system and '"found"' in system:
        return OOM_GATE
    return "BaselineOut" if '"citations"' in system and '"answer"' in system else ""


class FakeSend:
    """chat.completions.create 자리에 넣는 가짜 전송. 실제 API를 부르지 않는다.

    답변은 받은 messages의 해시로 정한다: 서버가 다른 요청의 내용을 프롬프트에 섞으면 답이 달라진다.
    받은 내용은 저장하지 않고 횟수와 해시, 낸 근거의 섹션 ID · 틀림 여부(`evidence`)만 남긴다
    (요청 내용 탐색에 걸리지 않게).

    - 기본: 항상 같은 모양(action answer, v2는 non_claim 한 문장, baseline은 첫 섹션 인용).
    - `vary=True`: 모양도 해시로 고른다(무작위 없음, 같은 프롬프트면 같은 답). 범주 · action ·
      문장 수 · claim 여부 · evidence 수 · 맞는/틀린 인용 문구 · 4,000자 초과 · 빈 답.
    - 모르는 스키마로 부르면 `UnknownSchema`를 던지고 `unknown`에 이름을 남긴다. 튜터는 전송
      예외를 오류 코드로 바꿔 삼키므로, `fake_send` 픽스처가 끝날 때 `unknown`이 비었는지 본다.
    - T 의도 판정(IntentOut, 튜터 #220 `toggles.intent_override`)에는
      {"label": `intent_label`, "self_info": false}로 답한다(기본 이름표 "학습 질문"은 덮지 않는다).
      `intent_fails`면 IntentOut 호출만 `IntentFailure`로 실패한다(`unknown`에는 넣지 않는다).
    - 자료 밖 확인 E(OomOut, 튜터 #232 `toggles.oom_gate`)에는
      {"asked": 표식, "quote": "", "found": `oom_found`}로 답한다. 기본 `oom_found=True`는 답을
      바꾸지 않는다(False면 최종 answer가 자료 밖 고정 문장이 된다).
    - `names`: 호출마다 요청한 스키마 이름(순서대로).
    """

    def __init__(self, *, vary: bool = False) -> None:
        self.vary = vary
        self.intent_label = INTENT_STUDY
        self.intent_fails = False
        self.oom_found = True
        self.names: list[str] = []
        self.count = 0
        self.digests: list[str] = []
        self.unknown: list[str] = []
        # 호출마다 하나씩(`count`와 길이가 같다). v2 주 호출의 vary 모드만 채운다(그 밖은 빈 기록)
        self.evidence: list[EvidenceLog] = []

    async def __call__(self, kwargs: dict[str, Any]) -> Any:
        self.count += 1
        messages = kwargs["messages"]
        # surrogatepass: 짝 없는 surrogate가 든 프롬프트도 해시한다(가짜가 먼저 죽지 않게)
        payload = json.dumps(messages, ensure_ascii=False, sort_keys=True).encode(
            "utf-8", "surrogatepass"
        )
        full = hashlib.sha256(payload)
        digest = full.hexdigest()[:16]
        self.digests.append(digest)
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        answer = f"{ANSWER_WORD} {digest}"
        picks = itertools.cycle(full.digest())  # 해시 바이트를 차례로 꺼내 모양을 고른다
        name = _schema_name(kwargs, system)
        self.names.append(name)
        log = NO_EVIDENCE
        if name == "IntentOut":  # T 의도 판정(tutor.judge.intent)
            if self.intent_fails:
                self.evidence.append(log)
                raise IntentFailure("가짜 의도 판정 실패")
            reply = {"label": self.intent_label, "self_info": False}
        elif name == OOM_GATE:  # 자료 밖 확인 E(tutor.judge.oom_gate)
            reply = {"asked": answer, "quote": "", "found": self.oom_found}
        elif name == "MainCallOut":  # v2 주 호출(tutor.judge.main_call)
            user_part = messages[-1]["content"] if messages else ""
            if self.vary:
                reply, log = _varied_main(answer, user_part, picks)
            else:
                reply = _plain_main(answer)
        elif name == "BaselineOut":  # 베이스라인(tutor.baseline): 튜터 규격 그대로
            sids = _SID_IN_PROMPT.findall(system)
            reply = (
                _varied_baseline(answer, sids, picks)
                if self.vary
                else _plain_baseline(answer, sids)
            )
        else:
            self.unknown.append(name)
            self.evidence.append(log)
            raise UnknownSchema(name)
        self.evidence.append(log)
        return _completion(json.dumps(reply, ensure_ascii=False))


def _completion(content: str) -> Any:
    """chat.completions.create 응답에서 서버가 읽는 부분만 가진 객체."""
    choice = types.SimpleNamespace(
        message=types.SimpleNamespace(content=content), finish_reason="stop"
    )
    return types.SimpleNamespace(choices=[choice], usage=types.SimpleNamespace(completion_tokens=5))


_GUARD_PROBE = [{"role": "user", "content": "ping"}]  # guardrail.llm.LLM.probe()가 보내는 요청
# json_object 방식에서 guardrail.llm.LLM._request가 system 끝에 붙이는 표식. 그 뒤가 스키마 JSON이다
_GUARD_SCHEMA_NOTE = "JSON 객체만 출력한다. 스키마:\n"


def _guard_models() -> dict[str, type[BaseModel]]:
    """`guardrail.judges.schemas`에 정의된 공개 pydantic 모델 전부(이름 → 모델).

    가드가 스키마를 더하거나 필드를 바꿔도 가짜가 따라간다. 이름이 "_"로 시작하는 것(`_Out`)과
    다른 모듈에서 가져온 이름(`BaseModel` 등)은 뺀다.
    """
    return {
        name: value
        for name, value in vars(guard_schemas).items()
        if not name.startswith("_")
        and isinstance(value, type)
        and issubclass(value, BaseModel)
        and value.__module__ == guard_schemas.__name__
    }


# 가짜가 아는 가드 판정 스키마(이름 → 모델). 테스트는 monkeypatch.setitem으로 임시 모델을 넣는다
GUARD_MODELS = _guard_models()


def _request_check_reply(note: str) -> dict[str, Any]:
    """RequestCheckOut 고정 응답. 문자열 필드에는 일반 경로처럼 판정 글 표식(`note`)을 넣는다
    (요청 점검 결과가 상태 · 로그로 새면 누출 탐색에 걸리게). kind "other"는 가드가 1차 범주를
    바꾸지 않는 종류다(request_category · recheck_block)."""
    return {
        "asks": [{"quote": note, "wants": note, "kind": "other"}],
        "evidence": note,
        "verdict": "not_asked",
    }


def _guard_fixed_replies() -> dict[str, tuple[type[BaseModel], Callable[[str], dict[str, Any]]]]:
    """`_guard_reply`의 일반 경로로 못 만드는 가드 스키마(이름 → (모델, note → 고정 응답)).

    - RequestCheckOut(가드 S3.13 요청 점검, `guardrail.judges.schemas_exp`, #229(요청 점검)에서
      기본 켬): `asks`가 중첩 목록이다. 종류 other 하나 · not_asked로 답한다(1차 범주를 두고,
      자료 밖으로 바꾸지 않는다). 그 모듈이나 이름이 없는 가드(#229 전)에서는 빈 표다.
    """
    try:
        from guardrail.judges.schemas_exp import RequestCheckOut
    except ImportError:
        return {}
    return {"RequestCheckOut": (RequestCheckOut, _request_check_reply)}


GUARD_FIXED_REPLIES = _guard_fixed_replies()
# json_object 방식에서 스키마를 알아보지 못했을 때 `FakeGuardSend.unknown`에 남기는 표시
UNKNOWN_JSON_OBJECT = "json_object:알 수 없는 스키마"


def _guard_schema_name(kwargs: dict[str, Any], system: str) -> str:
    """가드 판정 스키마 이름(guardrail.judges.schemas). 모르면 "".

    json_schema 방식이면 `response_format`의 이름으로 본다. json_object 방식이면 system 끝의
    마지막 표식 뒤를 JSON으로 읽어, 아는 스키마의 strict 스키마와 정확히 같을 때만 그 이름이다.
    system 본문의 낱말(예시 JSON의 필드 이름 등)로는 추측하지 않는다: 본문에 다른 스키마의 필드
    이름이 나오거나, 필드가 겹치는 새 스키마가 오면 엉뚱한 스키마로 답해 조용히 bad_json이 된다.
    """
    spec = kwargs.get("response_format", {}).get("json_schema")
    if isinstance(spec, dict):
        return str(spec.get("name", ""))
    _, found, tail = system.rpartition(_GUARD_SCHEMA_NOTE)
    if not found:
        return ""
    try:
        schema = json.loads(tail)
    except ValueError:
        return ""
    known = {**GUARD_MODELS, **{name: model for name, (model, _) in GUARD_FIXED_REPLIES.items()}}
    return next((name for name, model in known.items() if strict_schema(model) == schema), "")


class FakeGuardSend:
    """가드 LLM(`guardrail.llm.LLM`)의 chat.completions.create 자리에 넣는 가짜 전송.

    `FakeSend`의 가드 쪽 짝이다. 실제 API를 부르지 않고, 받은 내용은 저장하지 않는다(횟수 · 해시 ·
    스키마 이름만).

    - `count`는 판정 호출 수다. `LLM.start()`의 probe(요청이 "ping" 하나)는 `probes`에 따로 센다.
    - 응답은 그 스키마 모델의 필드에서 만든다(`_guard_reply`). 기본은 입력 판정 normal(act는
      learning_question), 출력 판정 grounded(· safe) true, 그 밖의 bool은 false다. 튜터 답을
      건드리지 않는 값이다.
    - `vary=True`: 범주 등 문자열 Literal과 grounded · safe를 프롬프트 해시로 고른다(무작위 없음).
    - `force`: 필드 이름 → 값. 그 필드가 모델에 있으면 이 값을 쓴다(vary보다 먼저). 예:
      `{"category": "jailbreak_injection"}`로 1차 입력 판정을 탈옥으로 만든다. 스키마에 맞지 않는
      값이면 검증 실패로 `unknown`에 남는다.
    - `GUARD_FIXED_REPLIES`의 스키마(RequestCheckOut)는 고정 응답에 `force`의 같은 이름 필드만
      덮어 답한다.
    - 모르는 스키마로 부르거나, 만든 응답이 모델 검증을 통과하지 못하면 `UnknownSchema`를 던지고
      `unknown`에 남긴다. 가짜 안에서 그 밖의 예외가 나도 "가짜 내부 오류: <클래스 이름>"으로
      남기고 다시 던진다. 가드 LLM은 전송 예외를 오류 코드로 바꿔 삼키므로, `fake_guard_send`
      픽스처가 끝날 때 `unknown`을 본다.
    """

    def __init__(self, *, vary: bool = False) -> None:
        self.vary = vary
        self.count = 0
        self.probes = 0
        self.clients = 0  # AsyncOpenAI 자리(OpenAIShim)에서 클라이언트를 만든 횟수
        self.by_schema: collections.Counter[str] = collections.Counter()
        self.digests: list[str] = []
        self.unknown: list[str] = []
        self.force: dict[str, Any] = {}

    async def __call__(self, kwargs: dict[str, Any]) -> Any:
        try:
            return await self._answer(kwargs)
        except UnknownSchema:
            raise  # `unknown`에 이미 남겼다
        except Exception as exc:
            # 가드 LLM이 삼키면 테스트가 조용히 지나간다. 종류 이름만 남긴다(요청 본문은 남기지
            # 않는다)
            self.unknown.append(f"가짜 내부 오류: {type(exc).__name__}")
            raise

    async def _answer(self, kwargs: dict[str, Any]) -> Any:
        messages = kwargs["messages"]
        if messages == _GUARD_PROBE:
            self.probes += 1
            return _completion("{}")
        self.count += 1
        payload = json.dumps(messages, ensure_ascii=False, sort_keys=True).encode(
            "utf-8", "surrogatepass"
        )
        full = hashlib.sha256(payload)
        digest = full.hexdigest()[:16]
        self.digests.append(digest)
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        name = _guard_schema_name(kwargs, system)
        self.by_schema[name] += 1
        picks = itertools.cycle(full.digest())
        note = f"{GUARD_WORD} {digest}"
        model = GUARD_MODELS.get(name)
        fixed = GUARD_FIXED_REPLIES.get(name)
        if model is None and fixed is not None:
            model, build = fixed
            # force 가운데 이 모델에 있는 필드(예: verdict)만 덮는다
            forced = {k: v for k, v in self.force.items() if k in model.model_fields}
            reply = {**build(note), **forced}
            try:
                model.model_validate(reply)
            except ValidationError:
                problem = f"{name}: 가짜 응답이 스키마와 맞지 않음"
                self.unknown.append(problem)
                raise UnknownSchema(problem) from None
            return _completion(json.dumps(reply, ensure_ascii=False))
        if model is None:
            # json_object 방식은 이름이 없다. 빈 문자열 대신 알아볼 수 있는 표시를 남긴다
            # (요청 본문은 남기지 않는다)
            self.unknown.append(name or UNKNOWN_JSON_OBJECT)
            raise UnknownSchema(name or UNKNOWN_JSON_OBJECT)
        reply = _guard_reply(model, note, picks, vary=self.vary, force=self.force)
        try:
            if reply is None:
                raise ValueError
            model.model_validate(reply)
        except (ValidationError, ValueError):
            # 가드가 필드를 바꿨는데 가짜가 따라가지 못했다. 옛 모양으로 답하면 가드 LLM이 조용히
            # bad_json이 되므로 여기서 드러낸다
            problem = f"{name}: 가짜 응답이 스키마와 맞지 않음"
            self.unknown.append(problem)
            raise UnknownSchema(problem) from None
        return _completion(json.dumps(reply, ensure_ascii=False))


# bool 필드의 기본값이 true인 이름(그 밖의 bool은 false). 튜터 답을 건드리지 않는 쪽이다
_TRUE_BY_DEFAULT = frozenset({"grounded", "safe"})
# vary=True일 때 해시로 고르는 bool: 이름 → 나눌 수(0이 아니면 true). grounded는 반반, safe는 3/4
_VARIED_BOOLS = {"grounded": 2, "safe": 4}
# 문자열 Literal 필드의 기본값: 튜터 답을 건드리지 않는 값(입력 판정 normal). act는 가드
# ACT_TO_CATEGORY로 normal이 된다(첫 값 no_meaningful_content는 low_signal_input이다). mode는 가드
# 입력 v2(InputModeOut)의 필드다. 모델에 없는 이름은 쓰이지 않을 뿐이다. 표에 없는 Literal 필드는
# 첫 값으로 채운다(가드가 새 판정 스키마를 올려도 tests/를 고치지 않고 돈다. 검증은 그대로 한다).
# verdict는 가드 #183 탈옥 2차 판정(JbCheckOut)의 필드다: study(공격 문구를 다루는 대상)는 범주를
# normal로 되돌려 튜터 답을 건드리지 않는다
_LITERAL_DEFAULTS = {
    "category": "normal",
    "act": "learning_question",
    "mode": "none",
    "verdict": "study",
}


def _guard_reply(
    model: type[BaseModel],
    note: str,
    picks: Iterator[int],
    *,
    vary: bool,
    force: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """스키마 모델의 필드를 차례로 돌며 가짜 응답을 만든다. 모르는 타입이 있으면 None.

    - `force`에 있는 필드는 그 값을 그대로 쓴다(타입 · vary와 무관, 맞는지는 호출자가 검증한다).
    - bool: grounded · safe는 true, 그 밖은 false. vary면 grounded · safe를 해시로 고른다.
    - str: 이름이 "_quote"로 끝나면 ""(인용할 구절 없음), 아니면 `note`(판정 글 표식 + 해시).
    - 문자열 Literal: `_LITERAL_DEFAULTS`의 값, 표에 없으면 첫 값. vary면 해시로 고른다.
    """
    reply: dict[str, Any] = {}
    force = force or {}
    for field, info in model.model_fields.items():
        kind = info.annotation
        if field in force:
            reply[field] = force[field]
        elif kind is bool:
            if vary and field in _VARIED_BOOLS:
                reply[field] = bool(next(picks) % _VARIED_BOOLS[field])
            else:
                reply[field] = field in _TRUE_BY_DEFAULT
        elif kind is str:
            reply[field] = "" if field.endswith("_quote") else note
        elif get_origin(kind) is Literal and all(isinstance(v, str) for v in get_args(kind)):
            values = get_args(kind)
            if vary:
                reply[field] = values[next(picks) % len(values)]
            else:
                default = _LITERAL_DEFAULTS.get(field)
                reply[field] = default if default in values else values[0]
        else:
            return None
    return reply


class OpenAIShim:
    """`guardrail.llm` 모듈의 `openai` 이름 자리에 넣는다.

    `AsyncOpenAI`만 가짜다: 만들어진 클라이언트의 `chat.completions.create`가 `FakeGuardSend`로
    간다. 그 밖의 이름(예외 클래스 · `Timeout`)은 진짜 openai로 넘긴다. 튜터가 쓰는 `openai`
    모듈 자체는 건드리지 않는다.
    """

    def __init__(self, fake: FakeGuardSend) -> None:
        self._fake = fake

    def __getattr__(self, name: str) -> Any:
        return getattr(openai, name)

    def AsyncOpenAI(self, **_: Any) -> Any:  # noqa: N802  SDK 클래스 이름 그대로
        fake = self._fake
        fake.clients += 1

        async def create(**kwargs: Any) -> Any:
            return await fake(kwargs)

        completions = types.SimpleNamespace(create=create)
        return types.SimpleNamespace(chat=types.SimpleNamespace(completions=completions))


def patched_guard_llm(
    monkeypatch: pytest.MonkeyPatch, env: Mapping[str, str]
) -> Generator[FakeGuardSend, None, None]:
    """가드 LLM의 전송을 가짜로 바꾸고 가짜를 내준 뒤, 끝날 때 모르는 호출이 없었는지 본다.

    conftest의 `fake_guard_send` 픽스처가 `yield from`으로 쓴다. 픽스처가 아닌 일반 제너레이터로
    빼 둔 것은 정리 단계의 단언을 테스트가 직접 돌려 볼 수 있게 하려는 것이다(픽스처의 정리 단계가
    실패를 내는지는 그 픽스처를 쓰는 테스트로는 확인할 수 없다).

    `env`는 패치된 `guardrail.llm.openai_env`가 돌려줄 값이다. 환경변수에는 넣지 않는다.
    """
    fake = FakeGuardSend()
    llm_mod = importlib.import_module("guardrail.llm")
    monkeypatch.setattr(llm_mod, "openai", OpenAIShim(fake))
    monkeypatch.setattr(llm_mod, "openai_env", lambda: dict(env))
    yield fake
    # 가드 LLM은 전송 예외를 오류 코드로 바꿔 삼킨다. 가짜가 모르는 호출이 있었으면 여기서 드러낸다.
    assert fake.unknown == [], (
        "FakeGuardSend가 모르는 스키마로 가드 LLM을 불렀다(applib.FakeGuardSend를 고친다)"
    )


def _plain_main(answer: str) -> dict[str, Any]:
    return {
        "category": "normal",
        "action": "answer",
        "sentences": [{"text": answer, "kind": "non_claim", "evidence": []}],
    }


def _plain_baseline(answer: str, sids: list[str]) -> dict[str, Any]:
    return {"answer": answer, "action": "answer", "citations": sids[:1]}


def _quote_from(body: str, pick: int) -> str:
    """섹션 본문에서 실제로 잘라 온 문구(8자 이상인 줄의 앞 40자)."""
    lines = [line.strip() for line in body.splitlines() if len(line.strip()) >= 8]
    return lines[pick % len(lines)][:40] if lines else body.strip()[:40]


def _varied_main(
    answer: str, user_part: str, picks: Iterator[int]
) -> tuple[dict[str, Any], EvidenceLog]:
    """MainCallOut 모양의 다양한 답: 범주 10종 × action 7종, 문장 0–3개, evidence 0–2개.

    낸 근거의 (sid, 틀림 여부)를 같이 돌려준다(테스트가 틀린 인용이 응답에 남았는지 본다).
    """
    sections = _SECTION_IN_PROMPT.findall(user_part)  # [(sid, 본문)]
    logged: list[tuple[str, bool]] = []
    category = CATEGORIES[next(picks) % len(CATEGORIES)]
    action = ACTIONS[next(picks) % len(ACTIONS)]
    shape = next(picks) % 8  # 0: 빈 문장 목록, 1: 4,000자 초과, 그 밖: 보통 길이
    sentences: list[dict[str, Any]] = []
    for index in range(0 if shape == 0 else 1 + next(picks) % 3):
        evidence = []
        for _ in range(next(picks) % 3 if sections else 0):
            sid, body = sections[next(picks) % len(sections)]
            wrong = next(picks) % 3 == 0  # 셋 중 하나는 원문에 없는 문구
            quote = f"원문에 없는 문구 {answer}" if wrong else _quote_from(body, next(picks))
            evidence.append({"sid": sid, "quote": quote})
            logged.append((sid, wrong))
        text = f"{answer} {index + 1}번째 문장이에요."
        if shape == 1 and index == 0:
            text += LONG_TAIL
        kind = "claim" if next(picks) % 4 else "non_claim"
        sentences.append({"text": text, "kind": kind, "evidence": evidence})
    reply = {"category": category, "action": action, "sentences": sentences}
    first_sid = sections[0][0] if sections else None
    return reply, EvidenceLog(first_sid=first_sid, items=tuple(logged))


def _varied_baseline(answer: str, sids: list[str], picks: Iterator[int]) -> dict[str, Any]:
    """BaselineOut 모양의 다양한 답: action 7종, 인용 0–3개(중복 ID · 없는 ID 포함)."""
    action = ACTIONS[next(picks) % len(ACTIONS)]
    first = sids[:1]
    second = sids[1:2] or first
    citations = [
        [],
        first,
        first + second,
        first + first,  # 중복 ID
        [MISSING_SIDS[0]],  # 없는 섹션뿐
        first + [MISSING_SIDS[1]] + second,
    ][next(picks) % 6]
    if next(picks) % 8 == 0:
        answer += LONG_TAIL
    return {"answer": answer, "action": action, "citations": citations}


# ── 요청 내용이 서버에 남았는지 찾기 ──────────────────────────────────────
#
# 보는 곳: 앱 상태, 우리 모듈의 전역 값, 우리 모듈에 정의된 클래스의 속성, 인스턴스 속성 · 슬롯,
# dict · list · tuple · set · deque, 함수의 클로저 · 기본 인자 · 함수 속성, 메서드 · partial ·
# property, StringIO · BytesIO. lru_cache는 내용을 볼 수 없어 `cache_sizes`가 항목 수로 본다.

OUR_PACKAGES = ("app", "tutor", "integration", "guardrail")
_SKIP_TYPES = (types.ModuleType, types.BuiltinFunctionType, int, float, bool, type(None))


def _ours(name: Any) -> bool:
    return isinstance(name, str) and (
        name in OUR_PACKAGES or name.startswith(tuple(f"{p}." for p in OUR_PACKAGES))
    )


def our_modules() -> list[types.ModuleType]:
    return [m for name, m in list(sys.modules.items()) if _ours(name) and m is not None]


def _children(obj: Any) -> list[tuple[Any, str]]:
    """obj에서 한 걸음에 닿는 값과 그 이름."""
    if isinstance(obj, types.FunctionType):
        out = [(obj.__defaults__, "<defaults>"), (obj.__kwdefaults__, "<kwdefaults>")]
        out.append((obj.__dict__, "<attrs>"))
        for cell in obj.__closure__ or ():
            try:
                out.append((cell.cell_contents, "<closure>"))
            except ValueError:  # 아직 값이 없는 셀
                pass
        return out
    if isinstance(obj, type):
        # 우리 모듈에 정의된 클래스만 속성을 본다(남의 클래스는 건너뛴다)
        if not _ours(getattr(obj, "__module__", None)):
            return []
        return [(v, k) for k, v in vars(obj).items()] + [(b, "<base>") for b in obj.__bases__]
    if isinstance(obj, staticmethod | classmethod):
        return [(obj.__func__, "<func>")]
    if isinstance(obj, property):
        return [(obj.fget, "<fget>"), (obj.fset, "<fset>"), (obj.fdel, "<fdel>")]
    if isinstance(obj, types.MethodType):
        return [(obj.__self__, "<self>"), (obj.__func__, "<func>")]
    if isinstance(obj, functools.partial):
        return [(obj.func, "<func>"), (obj.args, "<args>"), (obj.keywords, "<keywords>")]
    if isinstance(obj, io.StringIO | io.BytesIO):
        return [] if obj.closed else [(obj.getvalue(), "<buffer>")]
    if isinstance(obj, Mapping):
        out = []
        for k, v in list(obj.items()):
            out.append((k, "[key]"))
            out.append((v, f"[{k!r:.40}]"))
        return out
    if isinstance(obj, list | tuple | set | frozenset | collections.deque):
        return [(v, "[]") for v in list(obj)]
    out = [(type(obj), "<class>")]
    instance_dict = getattr(obj, "__dict__", None)
    if isinstance(instance_dict, dict):
        out.extend((v, f".{k}") for k, v in list(instance_dict.items()))
    for klass in type(obj).__mro__:
        slots = vars(klass).get("__slots__", ())
        for name in (slots,) if isinstance(slots, str) else slots:
            try:
                out.append((getattr(obj, name), f".{name}"))
            except AttributeError:
                pass
    return out


def find_text(roots: list[Any], needle: str | tuple[str, ...], limit: int = 3_000_000) -> list[str]:
    """roots에서 닿는 객체에서 needle(여러 개면 그중 하나)이 든 문자열 · 바이트의 위치."""
    needles = (needle,) if isinstance(needle, str) else tuple(needle)
    raw_needles = tuple(n.encode() for n in needles)
    hits: list[str] = []
    seen: set[int] = set()
    stack: list[tuple[Any, str]] = [(r, f"root{i}") for i, r in enumerate(roots)]
    while stack:
        obj, where = stack.pop()
        if isinstance(obj, str):
            if any(n in obj for n in needles):
                hits.append(where)
            continue
        if isinstance(obj, bytes | bytearray | memoryview):
            if any(n in bytes(obj) for n in raw_needles):
                hits.append(where)
            continue
        if isinstance(obj, _SKIP_TYPES) or id(obj) in seen:
            continue
        seen.add(id(obj))
        assert len(seen) < limit, "탐색 한도 초과"
        stack.extend((child, f"{where}{name}") for child, name in _children(obj))
    return hits


def state_roots(app: Any) -> list[Any]:
    """앱 상태와 우리 모듈의 전역 값(남의 모듈은 건너뛴다)."""
    roots: list[Any] = [app.state._state]
    for module in our_modules():
        roots.append({k: v for k, v in vars(module).items() if not k.startswith("__")})
    return roots


def _our_classes() -> list[type]:
    """우리 모듈에 정의된 클래스(클래스 안의 클래스 포함)."""
    found: dict[int, type] = {}
    stack = [v for m in our_modules() for v in vars(m).values()]
    while stack:
        value = stack.pop()
        if isinstance(value, type) and _ours(value.__module__) and id(value) not in found:
            found[id(value)] = value
            stack.extend(vars(value).values())
    return list(found.values())


def cache_sizes() -> dict[str, int]:
    """우리 모듈 · 클래스의 functools 캐시별 항목 수. 요청마다 늘면 요청 내용을 저장한 것이다."""
    sizes: dict[str, int] = {}

    def add(label: str, value: Any) -> None:
        value = getattr(value, "__func__", value)  # staticmethod · classmethod 안의 캐시
        info = getattr(value, "cache_info", None)
        if callable(info) and not isinstance(value, type):
            sizes[label] = info().currsize

    for module in our_modules():
        for name, value in list(vars(module).items()):
            if _ours(getattr(value, "__module__", None)):
                add(f"{module.__name__}.{name}", value)
    for klass in _our_classes():
        for name, value in list(vars(klass).items()):
            add(f"{klass.__module__}.{klass.__qualname__}.{name}", value)
    return sizes
