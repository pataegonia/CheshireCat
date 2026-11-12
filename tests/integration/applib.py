"""I1.4 조립 테스트 공용: 가짜 LLM 전송, 두 경로의 규격 검사, 요청 내용이 남았는지 찾는 탐색."""

import hashlib
import importlib.util
import json
import re
import sys
import types
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

OUR_PACKAGES = ("app", "tutor", "integration", "guardrail")
_SKIP_TYPES = (
    types.ModuleType,
    types.FunctionType,
    types.BuiltinFunctionType,
    type,
    int,
    float,
    bool,
    type(None),
)


def _ours(name: str) -> bool:
    return name in OUR_PACKAGES or name.startswith(tuple(f"{p}." for p in OUR_PACKAGES))


def our_modules() -> list[types.ModuleType]:
    return [m for name, m in list(sys.modules.items()) if _ours(name) and m is not None]


def find_text(roots: list[Any], needle: str, limit: int = 3_000_000) -> list[str]:
    """roots에서 닿는 객체(컨테이너 · 인스턴스 속성 · 클로저)에서 needle이 든 문자열의 위치."""
    hits: list[str] = []
    seen: set[int] = set()
    stack: list[tuple[Any, str]] = [(r, f"root{i}") for i, r in enumerate(roots)]
    visited = 0
    while stack:
        obj, where = stack.pop()
        if isinstance(obj, str):
            if needle in obj:
                hits.append(where)
            continue
        if isinstance(obj, bytes | bytearray):
            if needle.encode() in bytes(obj):
                hits.append(where)
            continue
        if isinstance(obj, types.FunctionType):
            for cell in obj.__closure__ or ():
                try:
                    stack.append((cell.cell_contents, f"{where}.<closure>"))
                except ValueError:
                    pass
            continue
        if isinstance(obj, _SKIP_TYPES) or id(obj) in seen:
            continue
        seen.add(id(obj))
        visited += 1
        assert visited < limit, "탐색 한도 초과"
        if isinstance(obj, dict):
            for k, v in obj.items():
                stack.append((k, f"{where}[key]"))
                stack.append((v, f"{where}[{k!r:.40}]"))
        elif isinstance(obj, list | tuple | set | frozenset):
            stack.extend((v, f"{where}[]") for v in obj)
        else:
            for name, value in getattr(obj, "__dict__", {}).items():
                stack.append((value, f"{where}.{name}"))
            for name in getattr(type(obj), "__slots__", ()):
                if hasattr(obj, name):
                    stack.append((getattr(obj, name), f"{where}.{name}"))
    return hits


def state_roots(app: Any) -> list[Any]:
    """앱 상태와 우리 모듈의 전역 값(모듈 · 클래스는 건너뛰고 함수는 클로저만 본다)."""
    roots: list[Any] = [app.state._state]
    for module in our_modules():
        roots.append({k: v for k, v in vars(module).items() if not k.startswith("__")})
    return roots


def cache_sizes() -> dict[str, int]:
    """우리 모듈의 functools 캐시 함수별 항목 수. 요청마다 늘면 요청 내용을 키로 저장한 것이다."""
    sizes: dict[str, int] = {}
    for module in our_modules():
        for name, value in vars(module).items():
            info = getattr(value, "cache_info", None)
            if callable(info) and getattr(value, "__module__", None) == module.__name__:
                sizes[f"{module.__name__}.{name}"] = info().currsize
    return sizes
