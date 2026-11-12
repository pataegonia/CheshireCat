"""I1.4 조립 테스트 공용: 가짜 LLM 전송, 두 경로의 규격 검사, 요청 내용이 남았는지 찾는 탐색."""

import collections
import functools
import hashlib
import importlib.util
import io
import json
import re
import sys
import types
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import jsonschema

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "integration-test-token"  # noqa: S105  테스트용 가짜 값
AUTH = {"Authorization": f"Bearer {TOKEN}"}

TUTOR_SCHEMA = json.loads((ROOT / "tutor/contract/tutor.schema.json").read_text("utf-8"))
_GUARD_CONTRACT = json.loads(
    (ROOT / "guardrail/docs/contract/guard.schema.json").read_text("utf-8")
)
GUARD_RESPONSE_SCHEMA = {"$ref": "#/$defs/response", "$defs": _GUARD_CONTRACT["$defs"]}

ANSWER_WORD = "가짜답변표식"  # 가짜 LLM이 답변에 넣는 단어(로그에 나오면 답변 본문이 샌 것)
_SID_IN_PROMPT = re.compile(r"\[((?:PY|PR|FIN)-\d{2}-\d{2})\]")


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


class FakeSend:
    """chat.completions.create 자리에 넣는 가짜 전송. 실제 API를 부르지 않는다.

    답변은 받은 messages의 해시로 정한다: 서버가 다른 요청의 내용을 프롬프트에 섞으면 답이 달라진다.
    받은 내용은 저장하지 않고 횟수와 해시만 남긴다(요청 내용 탐색에 걸리지 않게).
    """

    def __init__(self) -> None:
        self.count = 0
        self.digests: list[str] = []

    async def __call__(self, kwargs: dict[str, Any]) -> Any:
        self.count += 1
        messages = kwargs["messages"]
        payload = json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()
        digest = hashlib.sha256(payload).hexdigest()[:16]
        self.digests.append(digest)
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        cited = _SID_IN_PROMPT.search(system)
        content = json.dumps(
            {
                "answer": f"{ANSWER_WORD} {digest}",
                "action": "answer",
                "citations": [cited.group(1)] if cited else [],
            },
            ensure_ascii=False,
        )
        choice = types.SimpleNamespace(
            message=types.SimpleNamespace(content=content), finish_reason="stop"
        )
        return types.SimpleNamespace(
            choices=[choice], usage=types.SimpleNamespace(completion_tokens=5)
        )


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
