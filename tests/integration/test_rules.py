"""I1.4 실격 조항(PRD IN-11, SPEC §8): 루트 app.py가 만든 실제 app으로 확인한다.

- 요청 내용을 요청 사이에 저장 · 재사용하지 않는다(같은 요청 두 번 → LLM 두 번, 앞 요청이 뒤
  응답을 바꾸지 않음). 호출 횟수 카운터는 요청 내용이 없고 응답에 쓰이지 않아 대상이 아니다.
- `session_id` · 요청 헤더로 동작을 바꾸지 않는다(정적 검사: 소스를 파이썬으로 읽는다).

이 정적 검사는 리뷰 보조 수단이다. 철자를 바꾼 우회를 전부 막지는 못하고, 실제 보장은 동작
테스트(같은 요청 두 번, 응답 독립, 상태 탐색)가 한다.
"""

import ast
import collections
import functools
import importlib
import io
import json
import re
import sys
import types
from pathlib import Path

import pytest

from tests.integration.applib import (
    ANSWER_WORD,
    AUTH,
    GUARD_BUDGET_HINT,
    MAX_LLM_CALLS_PER_REQUEST,
    ROOT,
    as_bytes,
    cache_sizes,
    find_text,
    guard_call_budget,
    guard_calls_per_request,
    state_roots,
    user,
)

MARKER = "저장표식ZK4"
QUESTION_A = f"{MARKER} 변수 이름은 어떻게 지어?"
QUESTION_B = "예산을 세울 때 먼저 할 일이 뭐야?"


def post(client, path: str, body: dict, headers=AUTH):
    return client.post(path, content=as_bytes(body), headers=headers)


# ── 상태 · 캐시 없음 ──────────────────────────────────────────────────────


# TutorLLM.calls에 세는 역할 이름(모드별). 요청 하나에 LLM 호출은 한 번이다(세어 본 값).
LLM_ROLE = {"v2": "main", "baseline": "baseline"}
CALLS_PER_REQUEST = MAX_LLM_CALLS_PER_REQUEST  # 모드 → 횟수. 값은 applib 한 곳에 둔다


def test_same_request_twice_calls_the_llm_twice(llm_client, llm_mode, fake_send):
    first = post(llm_client, "/tutor", user(QUESTION_B))
    second = post(llm_client, "/tutor", user(QUESTION_B))
    assert fake_send.count == 2 * CALLS_PER_REQUEST[llm_mode]  # 캐시가 있으면 1이다
    assert fake_send.digests[0] == fake_send.digests[1]  # LLM이 받은 내용도 같다
    assert first.json() == second.json()
    assert first.json()["answer"].startswith(ANSWER_WORD)
    assert dict(llm_client.app.state.tutor_llm.calls) == {LLM_ROLE[llm_mode]: 2}


def test_one_request_makes_one_llm_call(llm_client, llm_mode, fake_send, fake_guard_send):
    for sent in range(1, 4):
        guard_before = fake_guard_send.count
        r = post(llm_client, "/tutor", user(f"{QUESTION_B} {sent}"))
        assert r.json()["answer"].startswith(ANSWER_WORD)  # 기본값이 아니라 LLM의 답이다
        assert fake_send.count == sent * CALLS_PER_REQUEST[llm_mode]
        # 튜터가 가드에 맡긴 판정(가드 LLM)도 이 요청 하나가 상한 안이고, 합계도 상한 안이다
        guard_used = fake_guard_send.count - guard_before
        assert guard_used <= guard_calls_per_request("/tutor"), GUARD_BUDGET_HINT
        assert fake_guard_send.count <= guard_call_budget(tutor=sent), GUARD_BUDGET_HINT
    assert dict(llm_client.app.state.tutor_llm.calls) == {LLM_ROLE[llm_mode]: 3}


def test_earlier_request_does_not_change_a_later_response(open_app, llm_mode, fake_send):
    with open_app() as alone:
        b_alone = post(alone, "/tutor", user(QUESTION_B)).json()
        digest_alone = fake_send.digests[-1]

    with open_app() as after:
        post(after, "/tutor", user(QUESTION_A))
        post(after, "/guard", {**user(QUESTION_A), "candidate_answer": f"{MARKER} 후보"})
        post(after, "/tutor", user(QUESTION_A), {"Authorization": "Bearer wrong"})
        b_after = post(after, "/tutor", user(QUESTION_B)).json()
        digest_after = fake_send.digests[-1]

    assert b_after == b_alone
    assert digest_after == digest_alone  # 앞 요청 내용이 뒤 요청의 프롬프트에 섞이지 않았다
    assert MARKER not in as_bytes(b_after).decode()


def test_guard_response_is_not_changed_by_earlier_requests(open_app, llm_mode):
    body = {**user(QUESTION_B), "candidate_answer": "예산은 계획이에요."}
    with open_app() as alone:
        g_alone = post(alone, "/guard", body).json()
    with open_app() as after:
        post(after, "/tutor", user(QUESTION_A))
        post(after, "/guard", {**user(QUESTION_A), "candidate_answer": MARKER})
        g_after = post(after, "/guard", body).json()
    assert g_after == g_alone


@pytest.mark.parametrize("session", ["s_1", "s_2", "", None, 7])
def test_session_id_does_not_change_the_response(llm_client, fake_send, session):
    base = post(llm_client, "/tutor", user(QUESTION_B)).json()
    body = user(QUESTION_B)
    if session is None:
        del body["session_id"]
    else:
        body["session_id"] = session
    assert post(llm_client, "/tutor", body).json() == base
    assert fake_send.digests[-1] == fake_send.digests[0]


@pytest.mark.parametrize(
    "extra",
    [
        {"User-Agent": "grader/1.0"},
        {"X-Grader": "1", "X-Request-Id": "abc", "X-Forwarded-For": "10.0.0.1"},
        {"Cookie": "session=abc"},
        {"Content-Type": "text/plain"},
    ],
)
def test_headers_other_than_auth_do_not_change_the_response(llm_client, fake_send, extra):
    base = post(llm_client, "/tutor", user(QUESTION_B)).json()
    assert post(llm_client, "/tutor", user(QUESTION_B), {**AUTH, **extra}).json() == base
    assert fake_send.digests[-1] == fake_send.digests[0]
    guard = post(llm_client, "/guard", user(QUESTION_B)).json()
    assert post(llm_client, "/guard", user(QUESTION_B), {**AUTH, **extra}).json() == guard


def kept_after_each_request(client) -> list[str]:
    """요청을 하나씩 보내고 **매번** 탐색한다. 남은 것의 위치 · 늘어난 캐시 이름을 돌려준다.

    끝에 한 번만 보면 뒤 요청이 앞 요청의 흔적을 덮어쓴 경우(`state.last = body`)를 놓친다.
    """
    sends = [
        lambda: post(client, "/tutor", user(QUESTION_A)),
        lambda: post(client, "/guard", {**user(QUESTION_A), "candidate_answer": f"{MARKER} 후보"}),
        lambda: post(client, "/tutor", {"messages": [{"role": "user", "content": MARKER}] * 9}),
        lambda: client.post("/tutor", content=b"not json " + MARKER.encode(), headers=AUTH),
        lambda: post(client, "/tutor", user(QUESTION_B)),  # 마지막은 표식이 없는 정상 요청
    ]
    found: list[str] = []
    caches_before = cache_sizes()
    for i, send in enumerate(sends):
        assert send().status_code == 200
        # 요청 문구(MARKER)와 답변 본문(ANSWER_WORD 뒤에 해시가 붙는다) 둘 다 찾는다
        hits = find_text(state_roots(client.app), (MARKER, ANSWER_WORD + " "))
        found.extend(f"요청{i}: {where}" for where in hits)
        caches = cache_sizes()
        found.extend(
            f"요청{i}: 캐시 {name}" for name in caches if caches[name] != caches_before.get(name, 0)
        )
        caches_before = caches
    return found


def test_request_content_is_not_kept_after_any_response(llm_client):
    assert find_text(state_roots(llm_client.app), MARKER) == []  # 요청 전에는 당연히 없다
    # 앱 상태 · 모듈 전역 · 클래스 속성 · 클로저 어디에도 요청 · 답변 내용이 남지 않고,
    # functools 캐시가 요청마다 늘지 않는다(기동 때 만든 것만 있다)
    assert kept_after_each_request(llm_client) == []


def test_search_helper_would_find_kept_content():
    """탐색 도구가 실제로 찾는지(위 테스트가 빈 결과로 통과하는 것이 아님을 확인)."""

    class Holder:
        def __init__(self):
            self.history = [{"q": ("x", MARKER + " 뒤")}]

    class Slotted:
        __slots__ = ("kept",)

        def __init__(self):
            self.kept = MARKER

    def closure():
        kept = [MARKER]
        return lambda: kept

    def default_arg(seen=[MARKER]):  # noqa: B006  기본 인자를 저장소로 쓰는 경우
        return seen

    def with_attr():
        return None

    with_attr.last = MARKER

    assert find_text([{"a": Holder()}], MARKER)
    assert find_text([{"a": Slotted()}], MARKER)
    assert find_text([{"f": closure()}], MARKER)
    assert find_text([{"f": default_arg}], MARKER)
    assert find_text([{"f": with_attr}], MARKER)
    assert find_text([{"m": Holder().__init__}], "없는말") == []
    assert find_text([{"p": functools.partial(closure, MARKER)}], MARKER)
    assert find_text([collections.deque([MARKER], maxlen=3)], MARKER)
    assert find_text([[b"raw " + MARKER.encode()]], MARKER)
    assert find_text([io.StringIO(MARKER)], MARKER)
    assert find_text([{"a": Holder()}], ("없는말", MARKER))
    assert find_text([{"a": Holder()}], "없는말") == []


# 탐색이 잡아야 하는 저장 방식(실제 튜터 코드는 고치지 않고, 처리 함수를 감싸 흉내 낸다)
LEAK_MODULE = "tutor._leak_probe"


def _leak_module() -> types.ModuleType:
    module = types.ModuleType(LEAK_MODULE)

    class Leaky:
        items: list = []  # (1) 클래스 속성 리스트

        @staticmethod
        @functools.lru_cache(maxsize=64)
        def lookup(query: str) -> int:  # (3) 클래스 staticmethod lru_cache
            return len(query)

    Leaky.__module__ = LEAK_MODULE
    module.Leaky = Leaky
    module.recent = collections.deque(maxlen=50)  # (2) 모듈 전역 deque
    return module


def _leak_class_attribute(module, state, text):
    module.Leaky.items.append(text)


def _leak_module_deque(module, state, text):
    module.recent.append(text)


def _leak_class_lru_cache(module, state, text):
    module.Leaky.lookup(text)


def _leak_app_state_last_body(module, state, text):
    state.last_body = text  # (4) 매 요청 덮어쓴다: 마지막 요청만 보면 놓친다


@pytest.mark.parametrize(
    "leak",
    [
        _leak_class_attribute,
        _leak_module_deque,
        _leak_class_lru_cache,
        _leak_app_state_last_body,
    ],
)
def test_state_search_catches_each_way_of_keeping_a_request(llm_client, monkeypatch, leak):
    module = _leak_module()
    monkeypatch.setitem(sys.modules, LEAK_MODULE, module)
    router_mod = importlib.import_module("tutor.router")
    real = router_mod.respond

    async def leaky(body, **kwargs):
        leak(module, kwargs["state"], json.dumps(body, ensure_ascii=False))
        return await real(body, **kwargs)

    assert kept_after_each_request(llm_client) == []  # 감싸기 전에는 깨끗하다
    monkeypatch.setattr(router_mod, "respond", leaky)
    found = kept_after_each_request(llm_client)
    assert found, leak.__name__
    assert any(f.startswith("요청0:") for f in found)  # 첫 요청 직후에 잡는다


# ── 정적 검사: session_id · 요청 헤더 ─────────────────────────────────────

# 개발용 코드(로컬 측정 · 지연 실험)는 대상이 아니다. tests/tutor는 이 폴더를 import하지만
# 서버 코드는 import하지 않는다(test_server_code_does_not_import_dev_only_code가 확인한다).
NOT_SERVER = ("tutor/eval/", "tutor/dev_only/")
DEV_ONLY_MODULES = ("tutor.eval", "tutor.dev_only")


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


SCANNED = sorted(
    p
    for p in [
        ROOT / "app.py",
        *(ROOT / "integration").rglob("*.py"),
        *(ROOT / "tutor").rglob("*.py"),
    ]
    if not _rel(p).startswith(NOT_SERVER)
)
# 어떤 객체에 붙어 있든 잡는 속성 이름
REQUEST_ATTRS = frozenset({"headers", "cookies", "query_params", "scope"})
# `.client`는 요청 객체에 붙었을 때만 잡는다(httpx.Client · self.client 같은 무관한 코드 제외)
REQUEST_NAMES = frozenset({"request", "req"})
# scope["headers"] · x.get("headers") 처럼 문자열 키로 꺼내는 경우
# ("client" 키는 넣지 않는다: 무관한 dict 키로 흔하고, 요청의 것은 `.scope`에서 먼저 걸린다)
REQUEST_KEYS = frozenset({"headers", "cookies", "cookie", "query_string"})
FASTAPI_PARAMS = frozenset({"Header", "Cookie"})
ALLOWLIST_HINT = (
    "허용 목록은 tests/integration/test_rules.py의 NOT_REQUEST_HEADERS에 사유와 함께 추가한다"
)

# 위 검사에 걸리지만 요청 헤더가 아닌 곳(파일, 함수). 사유를 적고, 늘릴 때는 리뷰한다.
NOT_REQUEST_HEADERS = {
    # OpenAI **응답**의 Retry-After · x-should-retry(재시도 대기 계산). 요청 헤더가 아니다.
    ("tutor/runtime/llm.py", "_fail"),
    # (#125부터 본체는 `_complete`, `complete`는 실패 집계만 한다)
    ("tutor/runtime/llm.py", "_complete"),
}
# 가드 서버에서 요청 · 응답 헤더를 읽는 함수와 헤더 이름(인증, 길이 계산뿐이어야 한다)
GUARD_HEADER_FUNCS = {"check_token", "_log_line", "_access_log"}
GUARD_HEADER_NAMES = {"authorization", "content-length"}


def _functions(tree: ast.AST) -> dict[int, str]:
    """노드 id → 그 노드를 감싼 가장 안쪽 함수 이름(없으면 <module>)."""
    owner: dict[int, str] = {}

    def walk(node: ast.AST, name: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = (
                child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else name
            )
            owner[id(child)] = inner
            walk(child, inner)

    walk(tree, "<module>")
    return owner


def _fold(node: ast.AST) -> str | None:
    """소스에 적힌 문자열 조각을 이어 붙인 결과: "a" + "b", f"a{x}b", "".join(["a", "b"])."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str | bytes):
        value = node.value
        return value if isinstance(value, str) else value.decode("utf-8", "replace")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _fold(node.left), _fold(node.right)
        return None if left is None and right is None else (left or "") + (right or "")
    if isinstance(node, ast.JoinedStr):
        return "".join(_fold(part) or "" for part in node.values)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.List | ast.Tuple)
    ):
        sep = _fold(node.func.value) or ""
        return sep.join(_fold(part) or "" for part in node.args[0].elts)
    return None


def session_id_reads(source: str) -> list[int]:
    """`session`이 든 이름 · 문자열을 쓰는 줄 번호.

    문자열은 이어 붙인 결과로 본다("sess" + "ion_id"). 독스트링과, 요청을 만드는 dict 리터럴의
    `"session_id"` 키는 읽기가 아니다.
    """
    tree = ast.parse(source)
    skipped = {
        id(key)
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for key in node.keys
        if isinstance(key, ast.Constant) and key.value == "session_id"
    }
    skipped |= {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    lines = []
    for node in ast.walk(tree):
        if id(node) in skipped:
            continue
        names = [_fold(node)]
        if isinstance(node, ast.Attribute):
            names.append(node.attr)
        elif isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.arg | ast.keyword):
            names.append(node.arg)
        elif isinstance(node, ast.alias):
            names += [node.name, node.asname]
        if any(name and "session" in name.casefold() for name in names):
            lines.append(node.lineno)
    return sorted(set(lines))


def _last_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    return node.attr if isinstance(node, ast.Attribute) else None


def request_attr_uses(source: str) -> list[tuple[str, str, int]]:
    """(함수, 무엇, 줄): 요청 헤더 · 쿠키 · 질의 · 접속 정보에 닿는 코드.

    `.headers` · `.cookies` · `.query_params` · `.scope`, `request.client`, 같은 이름의
    `getattr(x, "…")`, `x["headers"]` · `x.get("headers")`, fastapi의 `Header` · `Cookie`.
    """
    tree = ast.parse(source)
    owner = _functions(tree)
    uses: list[tuple[str, str, int]] = []

    def add(node: ast.AST, what: str) -> None:
        uses.append((owner[id(node)], what, node.lineno))

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr in REQUEST_ATTRS or node.attr in FASTAPI_PARAMS:
                add(node, node.attr)
            elif node.attr == "client" and _last_name(node.value) in REQUEST_NAMES:
                add(node, "client")
        elif isinstance(node, ast.Call) and _last_name(node.func) == "getattr":
            if len(node.args) < 2:
                continue
            on_request = _last_name(node.args[0]) in REQUEST_NAMES
            name = _fold(node.args[1])
            if name is None:
                if on_request:  # 이름을 실행 중에 만드는 경우는 무엇이든 잡는다
                    add(node, "getattr(request, ?)")
            elif name in REQUEST_ATTRS or (name == "client" and on_request):
                add(node, name)
        elif isinstance(node, ast.Call) and _last_name(node.func) == "get":
            key = _fold(node.args[0]) if node.args else None
            if key in REQUEST_KEYS:
                add(node, f"get({key})")
        elif isinstance(node, ast.Subscript):
            key = _fold(node.slice)
            if key in REQUEST_KEYS:
                add(node, f"[{key}]")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in ("fastapi", "starlette"):
                for alias in node.names:
                    if alias.name in FASTAPI_PARAMS:
                        add(node, f"import {alias.name}")
    return sorted(uses)


def print_calls(source: str) -> list[int]:
    return sorted(
        n.lineno
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print"
    )


def test_scan_covers_the_server_sources_only():
    names = {_rel(p) for p in SCANNED}
    for expected in (
        "app.py",
        "integration/actions.py",
        "integration/runtime.py",
        "tutor/router.py",
        "tutor/pipeline.py",
        "tutor/runtime/llm.py",
    ):
        assert expected in names
    assert len(SCANNED) >= 15
    assert not any(name.startswith(NOT_SERVER) for name in names)
    assert (ROOT / "tutor/eval").is_dir()  # 뺀 폴더가 실제로 있다(이름이 바뀌면 여기서 걸린다)
    assert (ROOT / "tutor/dev_only").is_dir()
    assert tuple(m.replace(".", "/") + "/" for m in DEV_ONLY_MODULES) == NOT_SERVER


def _is_dev_only(module: str) -> bool:
    return any(module == m or module.startswith(m + ".") for m in DEV_ONLY_MODULES)


def dev_only_imports(source: str, rel: str) -> list[tuple[str, int]]:
    """(모듈, 줄): `rel` 위치의 소스가 개발용 모듈(tutor.eval · tutor.dev_only)을 import하는 곳.

    `import a.b`, `from a import b`, 상대 import(`from .eval import x`, `from . import eval`),
    `import_module("…")` · `__import__("…")`(문자열은 이어 붙인 결과로 본다).
    """
    parts = rel.removesuffix(".py").split("/")
    package = parts[:-1]  # 이 파일이 속한 패키지(`__init__.py`도 같은 계산이다)
    found: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - (node.level - 1)] if node.level else []
            module = ".".join([*base, *((node.module or "").split(".") if node.module else [])])
            names = [module, *(f"{module}.{alias.name}" for alias in node.names)]
        elif isinstance(node, ast.Call) and _last_name(node.func) in (
            "import_module",
            "__import__",
        ):
            names = [text for arg in node.args[:1] if (text := _fold(arg)) is not None]
        found.extend((name, node.lineno) for name in names if _is_dev_only(name))
    return sorted(set(found))


def test_dev_only_import_check_catches_what_it_should():
    def hits(source: str, rel: str = "tutor/judge/flow.py") -> list[str]:
        return [name for name, _ in dev_only_imports(source, rel)]

    assert hits("import tutor.eval.run_local\n") == ["tutor.eval.run_local"]
    assert hits("import tutor.eval as e\n") == ["tutor.eval"]
    assert hits("from tutor.eval import run_local\n") == ["tutor.eval", "tutor.eval.run_local"]
    assert hits("from tutor import eval\n") == ["tutor.eval"]
    assert hits("from tutor import router, dev_only\n") == ["tutor.dev_only"]
    assert hits("from tutor.dev_only.tidy_latency import main\n") == [
        "tutor.dev_only.tidy_latency",
        "tutor.dev_only.tidy_latency.main",
    ]
    # 상대 import
    assert hits("from ..eval import run_local\n") == ["tutor.eval", "tutor.eval.run_local"]
    assert hits("from .. import eval\n") == ["tutor.eval"]
    assert hits("from .eval import x\n", "tutor/pipeline.py") == ["tutor.eval", "tutor.eval.x"]
    assert hits("from . import dev_only\n", "tutor/__init__.py") == ["tutor.dev_only"]
    # 함수 안의 늦은 import, 문자열로 import
    assert hits("def f():\n    from tutor.eval import recall\n") == [
        "tutor.eval",
        "tutor.eval.recall",
    ]
    assert hits('m = importlib.import_module("tutor.eval.recall")\n') == ["tutor.eval.recall"]
    assert hits('m = import_module("tutor." + "eval")\n') == ["tutor.eval"]
    assert hits('m = __import__("tutor.dev_only")\n') == ["tutor.dev_only"]

    # 다른 모듈은 잡지 않는다
    assert hits("from tutor.retrieval import retrieve\nimport tutor.router\n") == []
    assert hits("from .eval import x\n") == []  # tutor/judge/eval 은 다른 모듈이다
    assert hits("from tutor.evaluate import x\nimport tutor.evals\n") == []
    assert hits("from guardrail.eval.cv import load_splits\n") == []  # 가드의 eval은 대상이 아니다
    assert hits('m = importlib.import_module("tutor.router")\n') == []
    assert hits('"""python -m tutor.dev_only.tidy_latency 로 잰다."""\n') == []


def test_server_code_does_not_import_dev_only_code():
    """정적 검사에서 뺀 폴더(NOT_SERVER)를 서버 코드가 불러 쓰면 그 코드도 서버 코드가 된다."""
    found = {
        _rel(p): hits for p in SCANNED if (hits := dev_only_imports(p.read_text("utf-8"), _rel(p)))
    }
    assert found == {}


def test_no_code_reads_session_id():
    found = {_rel(p): lines for p in SCANNED if (lines := session_id_reads(p.read_text("utf-8")))}
    assert found == {}, "서버 코드는 session_id를 읽지 않는다(허용 목록 없음, PRD IN-11)"


def test_no_code_reads_request_headers_outside_auth_and_length():
    found = [
        (_rel(p), func, what, line)
        for p in SCANNED
        for func, what, line in request_attr_uses(p.read_text("utf-8"))
        if (_rel(p), func) not in NOT_REQUEST_HEADERS
    ]
    assert found == [], ALLOWLIST_HINT


def test_allowed_header_uses_still_exist_and_do_not_touch_the_request():
    """허용 목록이 낡지 않았고, 그 자리에서 읽는 것이 `request`가 아니다."""
    for rel, func in sorted(NOT_REQUEST_HEADERS):
        source = (ROOT / rel).read_text("utf-8")
        uses = [u for u in request_attr_uses(source) if u[0] == func]
        assert uses, f"{rel}:{func} 는 이제 걸리지 않는다. 허용 목록에서 지운다"
        assert {what for _, what, _ in uses} == {"headers"}, (rel, func, ALLOWLIST_HINT)
        tree = ast.parse(source)
        owner = _functions(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "headers"
                and owner[id(node)] == func
            ):
                root = node.value
                while isinstance(root, ast.Attribute):
                    root = root.value
                assert isinstance(root, ast.Name) and root.id not in REQUEST_NAMES, (
                    rel,
                    func,
                    ALLOWLIST_HINT,
                )


def test_guard_server_reads_headers_only_for_auth_and_length():
    source = (ROOT / "guardrail/src/guardrail/server.py").read_text("utf-8")
    uses = request_attr_uses(source)
    assert {what for _, what, _ in uses} == {"headers"}
    assert {func for func, _, _ in uses} == GUARD_HEADER_FUNCS
    names = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "headers"
        ):
            assert isinstance(node.args[0], ast.Constant)
            names.add(node.args[0].value)
    assert names == GUARD_HEADER_NAMES
    assert session_id_reads(source) == []


def test_static_checks_catch_what_they_should():
    """검사기 자체 확인: 읽기는 잡고, 요청을 만드는 dict 키 · 무관한 코드는 잡지 않는다."""
    assert session_id_reads('x = body["session_id"]\n') == [1]
    assert session_id_reads('x = body.get("session_id")\n') == [1]
    assert session_id_reads("x = req.session_id\n") == [1]
    assert session_id_reads("def f(session_id):\n    return 1\n") == [1]
    assert session_id_reads('body = {"session_id": s, "messages": m}\n') == []
    assert session_id_reads('"""session_id는 보지 않는다."""\n') == []
    assert session_id_reads('def f():\n    """session_id 설명."""\n    return 1\n') == []
    # 문자열 조각을 이어 붙이는 경우
    assert session_id_reads('x = body["session" + "_id"]\n') == [1]
    assert session_id_reads('x = body["sess" + "ion_id"]\n') == [1]
    assert session_id_reads('x = body.get("session" + suffix)\n') == [1]
    assert session_id_reads('x = body[f"session_{name}"]\n') == [1]
    assert session_id_reads('x = body["".join(["sess", "ion", "_id"])]\n') == [1]
    assert session_id_reads('x = body["SESSION_ID".lower()]\n') == [1]
    assert session_id_reads('x = body[b"session_id"]\n') == [1]
    assert session_id_reads('x = body["messages"]\n') == []

    def uses(source: str) -> list[tuple[str, str]]:
        return [(func, what) for func, what, _ in request_attr_uses(source)]

    assert request_attr_uses("def f(request):\n    return request.headers.get('x')\n") == [
        ("f", "headers", 2)
    ]
    assert uses("async def g(r):\n    return r.cookies, r.query_params\n") == [
        ("g", "cookies"),
        ("g", "query_params"),
    ]
    # request.scope["headers"] · getattr · fastapi Header/Cookie
    assert uses('def f(request):\n    return request.scope["headers"]\n') == [
        ("f", "[headers]"),
        ("f", "scope"),
    ]
    assert uses('def f(scope):\n    return scope["headers"], scope.get("cookie")\n') == [
        ("f", "[headers]"),
        ("f", "get(cookie)"),
    ]
    assert uses('def f(x):\n    return getattr(x, "headers")\n') == [("f", "headers")]
    assert uses('def f(x):\n    return getattr(x, "head" + "ers", None)\n') == [("f", "headers")]
    assert uses("def f(request, name):\n    return getattr(request, name)\n") == [
        ("f", "getattr(request, ?)")
    ]
    assert uses("from fastapi import APIRouter, Header\n") == [("<module>", "import Header")]
    assert uses("from fastapi.params import Cookie as C\n") == [("<module>", "import Cookie")]
    assert uses("import fastapi\n\n\ndef f(a=fastapi.Header(None)):\n    return a\n") == [
        ("f", "Header")
    ]
    # .client는 요청 객체일 때만
    assert uses("def f(request):\n    return request.client.host\n") == [("f", "client")]
    assert uses("def f(self):\n    return self.request.client\n") == [("f", "client")]
    assert uses('def f(request):\n    return getattr(request, "client")\n') == [("f", "client")]
    assert (
        uses("def f(self):\n    return self.client.chat, httpx.Client(), state.tutor_client\n")
        == []
    )
    assert uses('def f(x):\n    return getattr(x, "client", None), getattr(x, "cfg")\n') == []
    assert uses("from fastapi import APIRouter, Request\n") == []
    assert uses('def f(made):\n    return made["client"].chat\n') == []

    assert print_calls("def f():\n    print(1)\n") == [2]
    assert print_calls("def f():\n    return 'print(1)'\n") == []


def test_no_print_in_server_code():
    """app.py · integration · tutor 서버 코드(tutor/eval · tutor/dev_only 제외)."""
    found = {_rel(p): lines for p in SCANNED if (lines := print_calls(p.read_text("utf-8")))}
    assert found == {}


# ── 정적 검사: 파일 쓰기 ──────────────────────────────────────────────────
# 요청 본문을 파일에 남기면 상태 탐색(메모리만 본다)과 로그 검사에 걸리지 않는다. 서버 코드는
# 파일을 읽기만 한다.

OS_WRITE_FUNCS = frozenset({"open", "write", "fdopen"})  # os.open · os.write · os.fdopen
PATH_WRITE_METHODS = frozenset({"write_text", "write_bytes"})
OPEN_MODULES = frozenset({"io", "codecs", "builtins"})  # open(file, mode) 꼴인 모듈 함수
_MODE = re.compile(r"[rwaxbtU+]{1,4}")


def _open_mode(call: ast.Call) -> ast.AST | None:
    """open 호출의 mode 인자 노드(없으면 None = 읽기)."""
    for keyword in call.keywords:
        if keyword.arg == "mode":
            return keyword.value
    func = call.func
    module_level = isinstance(func, ast.Name) or (
        isinstance(func, ast.Attribute) and _last_name(func.value) in OPEN_MODULES
    )
    if module_level:  # open(file, mode)
        return call.args[1] if len(call.args) > 1 else None
    # path.open(mode): 첫 인자가 mode처럼 생겼을 때만 mode로 본다(zf.open("a.txt")는 파일 이름)
    first = call.args[0] if call.args else None
    return first if first is not None and _MODE.fullmatch(_fold(first) or "") else None


def file_write_uses(source: str) -> list[tuple[str, int]]:
    """(무엇, 줄): 파일에 쓸 수 있는 코드.

    `os.open` · `os.write` · `os.fdopen`(이름을 꺼내 쓰는 것, `from os import …` 포함),
    `.write_text` · `.write_bytes`, 쓰기 모드(w · a · x · +)이거나 mode를 실행 중에 정하는 open.
    mode가 없는 open과 `read_text`는 읽기라 잡지 않는다.
    """
    uses: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute):
            if node.attr in OS_WRITE_FUNCS and _last_name(node.value) == "os":
                uses.append((f"os.{node.attr}", node.lineno))
            elif node.attr in PATH_WRITE_METHODS:
                uses.append((node.attr, node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            uses.extend(
                (f"from os import {alias.name}", node.lineno)
                for alias in node.names
                if alias.name in OS_WRITE_FUNCS or alias.name == "*"
            )
        elif isinstance(node, ast.Call) and _last_name(node.func) == "open":
            if isinstance(node.func, ast.Attribute) and _last_name(node.func.value) == "os":
                continue  # os.open은 위에서 잡았다
            mode = _open_mode(node)
            if mode is None:
                continue
            text = _fold(mode)
            if text is None:
                uses.append(("open(mode=?)", node.lineno))
            elif set(text) & set("wax+"):
                uses.append((f"open({text})", node.lineno))
    return sorted(uses)


def test_file_write_check_catches_what_it_should():
    """검사기 자체 확인: 쓰기는 잡고 읽기는 잡지 않는다."""

    def uses(source: str) -> list[str]:
        return [what for what, _ in file_write_uses(source)]

    # 배경이 된 경우: 요청 경로에서 os.open + os.write로 본문을 파일에 쓴다
    leak = (
        "import os\n\n\n"
        "async def respond(body):\n"
        '    fd = os.open("/tmp/bodies", os.O_WRONLY | os.O_APPEND | os.O_CREAT)\n'
        "    os.write(fd, repr(body).encode())\n"
        "    os.close(fd)\n"
    )
    assert file_write_uses(leak) == [("os.open", 5), ("os.write", 6)]
    assert uses("import os\nf = os.fdopen(3, 'w')\n") == ["os.fdopen"]
    assert uses("import os\nw = os.write\n") == ["os.write"]  # 이름만 꺼내도 잡는다
    assert uses("from os import write, getcwd\n") == ["from os import write"]
    assert uses("from os import *\n") == ["from os import *"]
    assert uses('p.write_text("x")\n') == ["write_text"]
    assert uses("Path(a).write_bytes(b)\n") == ["write_bytes"]
    assert uses("w = path.write_text\n") == ["write_text"]
    for mode in ("w", "a", "x", "wb", "ab", "r+", "w+", "rb+", "at"):
        assert uses(f'f = open(name, "{mode}")\n') == [f"open({mode})"], mode
        assert uses(f'f = open(name, mode="{mode}")\n') == [f"open({mode})"], mode
        assert uses(f'f = path.open("{mode}")\n') == [f"open({mode})"], mode
        assert uses(f'f = path.open(mode="{mode}")\n') == [f"open({mode})"], mode
        assert uses(f'f = io.open(name, "{mode}")\n') == [f"open({mode})"], mode
    assert uses('with open(name, "w", encoding="utf-8") as f:\n    f.write(x)\n') == ["open(w)"]
    assert uses('f = open(name, "r" + "+")\n') == ["open(r+)"]
    assert uses("f = open(name, mode)\n") == ["open(mode=?)"]
    assert uses("f = path.open(mode=m)\n") == ["open(mode=?)"]

    # 읽기는 허용
    assert uses("f = open(name)\n") == []
    assert uses('f = open(name, "r")\n') == []
    assert uses('f = open(name, "rb")\n') == []
    assert uses('f = open(name, mode="rt", encoding="utf-8")\n') == []
    assert uses('f = open(name, encoding="utf-8")\n') == []
    assert uses('f = path.open()\nf = path.open("rb")\nf = path.open(encoding="utf-8")\n') == []
    assert uses('text = path.read_text("utf-8")\nraw = path.read_bytes()\n') == []
    assert uses('f = archive.open("data.txt")\n') == []  # 첫 인자가 파일 이름
    assert uses("import os\nx = os.environ.get('A')\ny = os.path.join(a, b)\n") == []
    assert uses("buf.write(x)\nsys.stdout.write(x)\n") == []  # 파일 객체의 write는 open에서 잡는다
    assert uses('"""os.write(fd, body) 는 쓰지 않는다."""\n') == []


def test_no_file_writes_in_server_code():
    """app.py · integration · tutor 서버 코드는 파일에 쓰지 않는다(허용 목록 없음)."""
    found = {_rel(p): uses for p in SCANNED if (uses := file_write_uses(p.read_text("utf-8")))}
    assert found == {}
