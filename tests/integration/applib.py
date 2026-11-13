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
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, NamedTuple, get_args

import jsonschema
import openai
from guardrail.schema import Category

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


# 요청 하나에 튜터가 **자기 전송 함수로** LLM을 부르는 횟수의 상한(모드별). 튜터가 자기 호출을
# 늘리면 여기 한 곳과 FakeSend의 응답(스키마 이름별 분기)을 고친다. 튜터가 가드에 맡기는 판정
# (classify · judge_output, #88)은 튜터 전송 함수를 지나지 않는다: 아래 가드 쪽 상한에서 센다.
MAX_LLM_CALLS_PER_REQUEST = {"v2": 1, "baseline": 1}

# 요청 하나에 가드 LLM(`guardrail.llm.LLM`)이 nano를 부르는 횟수의 상한(경로별). probe는 세지
# 않는다(기동 때 한 번). 지금 루트 앱의 가드는 상수 판정기라 0이다. 가드가 기본 판정기를 실제
# 판정기로 바꾸면(S2.8) 여기 한 곳을 고친다: `/guard`는 입력 판정 + 출력 판정(+ 재시도),
# `/tutor`는 튜터가 맡기는 classify · judge_output. 요청당 전체 nano 호출 상한은 두 표의 합이다.
MAX_GUARD_LLM_CALLS_PER_REQUEST = {"/tutor": 0, "/guard": 0}
GUARD_BUDGET_HINT = (
    "가드 nano 호출이 상한을 넘었다. 판정기를 바꿨으면 "
    "applib.MAX_GUARD_LLM_CALLS_PER_REQUEST를 고친다"
)


def guard_call_budget(*, tutor: int = 0, guard: int = 0) -> int:
    """`/tutor` 요청 tutor개와 `/guard` 요청 guard개에 허용되는 가드 nano 호출 수."""
    limits = MAX_GUARD_LLM_CALLS_PER_REQUEST
    return tutor * limits["/tutor"] + guard * limits["/guard"]


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


def _schema_name(kwargs: dict[str, Any], system: str) -> str:
    """요청한 응답 스키마 이름. json_schema 방식이면 이름으로, json_object 방식이면 system에
    붙은 스키마의 필드로 알아본다."""
    spec = kwargs.get("response_format", {}).get("json_schema")
    if isinstance(spec, dict):
        return str(spec.get("name", ""))
    if '"sentences"' in system and '"category"' in system:
        return "MainCallOut"
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
    """

    def __init__(self, *, vary: bool = False) -> None:
        self.vary = vary
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
        log = NO_EVIDENCE
        if name == "MainCallOut":  # v2 주 호출(tutor.judge.main_call)
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


def _guard_schema_name(kwargs: dict[str, Any], system: str) -> str:
    """가드 판정 스키마 이름(guardrail.judges.schemas). json_schema 방식이면 이름으로,
    json_object 방식이면 system 끝에 붙은 스키마의 필드로 알아본다."""
    spec = kwargs.get("response_format", {}).get("json_schema")
    if isinstance(spec, dict):
        return str(spec.get("name", ""))
    if '"category"' in system and '"evidence"' in system:
        return "InputOut"
    if '"grounded"' in system and '"reason"' in system:
        return "OutputBaselineOut" if '"safe"' in system else "OutputOut"
    return ""


class FakeGuardSend:
    """가드 LLM(`guardrail.llm.LLM`)의 chat.completions.create 자리에 넣는 가짜 전송.

    `FakeSend`의 가드 쪽 짝이다. 실제 API를 부르지 않고, 받은 내용은 저장하지 않는다(횟수 · 해시 ·
    스키마 이름만).

    - `count`는 판정 호출 수다. `LLM.start()`의 probe(요청이 "ping" 하나)는 `probes`에 따로 센다.
    - 기본: 입력 판정은 normal, 출력 판정은 grounded(· safe) true. 튜터 답을 건드리지 않는 값이다.
    - `vary=True`: 범주 10종과 grounded · safe를 프롬프트 해시로 고른다(무작위 없음).
    - 모르는 스키마로 부르면 `UnknownSchema`를 던지고 `unknown`에 이름을 남긴다. 가드 LLM은 전송
      예외를 오류 코드로 바꿔 삼키므로, `fake_guard_send` 픽스처가 끝날 때 `unknown`을 본다.
    """

    def __init__(self, *, vary: bool = False) -> None:
        self.vary = vary
        self.count = 0
        self.probes = 0
        self.clients = 0  # AsyncOpenAI 자리(OpenAIShim)에서 클라이언트를 만든 횟수
        self.by_schema: collections.Counter[str] = collections.Counter()
        self.digests: list[str] = []
        self.unknown: list[str] = []

    async def __call__(self, kwargs: dict[str, Any]) -> Any:
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
        if name == "InputOut":
            category = CATEGORIES[next(picks) % len(CATEGORIES)] if self.vary else "normal"
            reply: dict[str, Any] = {"evidence": note, "category": category}
        elif name in ("OutputOut", "OutputBaselineOut"):
            reply = {"reason": note, "grounded": bool(next(picks) % 2) if self.vary else True}
            if name == "OutputBaselineOut":
                reply["safe"] = bool(next(picks) % 4) if self.vary else True
        else:
            self.unknown.append(name)
            raise UnknownSchema(name)
        return _completion(json.dumps(reply, ensure_ascii=False))


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
