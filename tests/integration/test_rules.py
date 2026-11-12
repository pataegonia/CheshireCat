"""I1.4 실격 조항(PRD IN-11, SPEC §8): 루트 app.py가 만든 실제 app으로 확인한다.

- 요청 내용을 요청 사이에 저장 · 재사용하지 않는다(같은 요청 두 번 → LLM 두 번, 앞 요청이 뒤
  응답을 바꾸지 않음). 호출 횟수 카운터는 요청 내용이 없고 응답에 쓰이지 않아 대상이 아니다.
- `session_id` · 요청 헤더로 동작을 바꾸지 않는다(정적 검사: 소스를 파이썬으로 읽는다).
"""

import ast
from pathlib import Path

import pytest

from tests.integration.applib import (
    ANSWER_WORD,
    AUTH,
    ROOT,
    as_bytes,
    cache_sizes,
    find_text,
    state_roots,
    user,
)

MARKER = "저장표식ZK4"
QUESTION_A = f"{MARKER} 변수 이름은 어떻게 지어?"
QUESTION_B = "예산을 세울 때 먼저 할 일이 뭐야?"


def post(client, path: str, body: dict, headers=AUTH):
    return client.post(path, content=as_bytes(body), headers=headers)


# ── 상태 · 캐시 없음 ──────────────────────────────────────────────────────


def test_same_request_twice_calls_the_llm_twice(baseline_client, fake_send):
    first = post(baseline_client, "/tutor", user(QUESTION_B))
    second = post(baseline_client, "/tutor", user(QUESTION_B))
    assert fake_send.count == 2  # 캐시가 있으면 1이다
    assert fake_send.digests[0] == fake_send.digests[1]  # LLM이 받은 내용도 같다
    assert first.json() == second.json()
    assert first.json()["answer"].startswith(ANSWER_WORD)
    assert dict(baseline_client.app.state.tutor_llm.calls) == {"baseline": 2}


def test_earlier_request_does_not_change_a_later_response(open_app, baseline_mode, fake_send):
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


def test_guard_response_is_not_changed_by_earlier_requests(open_app, baseline_mode):
    body = {**user(QUESTION_B), "candidate_answer": "예산은 계획이에요."}
    with open_app() as alone:
        g_alone = post(alone, "/guard", body).json()
    with open_app() as after:
        post(after, "/tutor", user(QUESTION_A))
        post(after, "/guard", {**user(QUESTION_A), "candidate_answer": MARKER})
        g_after = post(after, "/guard", body).json()
    assert g_after == g_alone


@pytest.mark.parametrize("session", ["s_1", "s_2", "", None, 7])
def test_session_id_does_not_change_the_response(baseline_client, fake_send, session):
    base = post(baseline_client, "/tutor", user(QUESTION_B)).json()
    body = user(QUESTION_B)
    if session is None:
        del body["session_id"]
    else:
        body["session_id"] = session
    assert post(baseline_client, "/tutor", body).json() == base
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
def test_headers_other_than_auth_do_not_change_the_response(baseline_client, fake_send, extra):
    base = post(baseline_client, "/tutor", user(QUESTION_B)).json()
    assert post(baseline_client, "/tutor", user(QUESTION_B), {**AUTH, **extra}).json() == base
    assert fake_send.digests[-1] == fake_send.digests[0]
    guard = post(baseline_client, "/guard", user(QUESTION_B)).json()
    assert post(baseline_client, "/guard", user(QUESTION_B), {**AUTH, **extra}).json() == guard


def test_request_content_is_not_kept_after_the_response(baseline_client):
    roots = state_roots(baseline_client.app)
    assert find_text(roots, MARKER) == []  # 요청 전에는 당연히 없다
    caches_before = cache_sizes()

    post(baseline_client, "/tutor", user(QUESTION_A))
    post(baseline_client, "/guard", {**user(QUESTION_A), "candidate_answer": f"{MARKER} 후보"})
    post(baseline_client, "/tutor", {"messages": [{"role": "user", "content": MARKER}] * 9})
    baseline_client.post("/tutor", content=b"not json " + MARKER.encode(), headers=AUTH)

    # 앱 상태 · 모듈 전역 · 클로저 어디에도 요청 내용이 남지 않았다
    assert find_text(state_roots(baseline_client.app), MARKER) == []
    # 답변 본문도 남기지 않는다
    assert find_text(state_roots(baseline_client.app), ANSWER_WORD + " ") == []
    # functools 캐시가 요청마다 늘지 않았다(기동 때 만든 것만 있다)
    assert cache_sizes() == caches_before


def test_search_helper_would_find_kept_content():
    """탐색 도구가 실제로 찾는지(위 테스트가 빈 결과로 통과하는 것이 아님을 확인)."""

    class Holder:
        def __init__(self):
            self.history = [{"q": ("x", MARKER + " 뒤")}]

    def closure():
        kept = [MARKER]
        return lambda: kept

    assert find_text([{"a": Holder()}], MARKER)
    assert find_text([{"f": closure()}], MARKER)
    assert find_text([[b"raw " + MARKER.encode()]], MARKER)
    assert find_text([{"a": Holder()}], "없는말") == []


# ── 정적 검사: session_id · 요청 헤더 ─────────────────────────────────────

SCANNED = sorted(
    [ROOT / "app.py", *(ROOT / "integration").rglob("*.py"), *(ROOT / "tutor").rglob("*.py")]
)
REQUEST_ATTRS = frozenset({"headers", "cookies", "query_params", "client"})

# `.headers`를 쓰지만 요청 헤더가 아닌 곳(파일, 함수). 사유를 적고, 늘릴 때는 리뷰한다.
NOT_REQUEST_HEADERS = {
    # OpenAI **응답**의 Retry-After · x-should-retry(재시도 대기 계산). 요청 헤더가 아니다.
    ("tutor/runtime/llm.py", "_fail"),
    ("tutor/runtime/llm.py", "complete"),
}
# 가드 서버에서 요청 · 응답 헤더를 읽는 함수와 헤더 이름(인증, 길이 계산뿐이어야 한다)
GUARD_HEADER_FUNCS = {"check_token", "_log_line", "_access_log"}
GUARD_HEADER_NAMES = {"authorization", "content-length"}


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


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


def session_id_reads(source: str) -> list[int]:
    """`session_id`를 읽는 줄 번호. dict 리터럴의 키(요청을 만드는 쪽)는 읽기가 아니다."""
    tree = ast.parse(source)
    built_keys = {
        id(key) for node in ast.walk(tree) if isinstance(node, ast.Dict) for key in node.keys
    }
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "session_id":
            if id(node) not in built_keys:
                lines.append(node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr == "session_id":
            lines.append(node.lineno)
        elif isinstance(node, ast.Name) and node.id == "session_id":
            lines.append(node.lineno)
        elif isinstance(node, ast.arg) and node.arg == "session_id":
            lines.append(node.lineno)
        elif isinstance(node, ast.keyword) and node.arg == "session_id":
            lines.append(node.lineno)
    return sorted(set(lines))


def request_attr_uses(source: str) -> list[tuple[str, str, int]]:
    """(함수, 속성, 줄): `.headers` · `.cookies` · `.query_params` · `.client` 접근."""
    tree = ast.parse(source)
    owner = _functions(tree)
    return sorted(
        (owner[id(node)], node.attr, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in REQUEST_ATTRS
    )


def test_scan_covers_the_server_sources():
    names = {_rel(p) for p in SCANNED}
    for expected in ("app.py", "integration/actions.py", "tutor/router.py", "tutor/pipeline.py"):
        assert expected in names
    assert len(SCANNED) >= 20


def test_no_code_reads_session_id():
    found = {_rel(p): lines for p in SCANNED if (lines := session_id_reads(p.read_text("utf-8")))}
    assert found == {}


def test_no_code_reads_request_headers_outside_auth_and_length():
    found = [
        (_rel(p), func, attr, line)
        for p in SCANNED
        for func, attr, line in request_attr_uses(p.read_text("utf-8"))
        if (_rel(p), func) not in NOT_REQUEST_HEADERS
    ]
    assert found == []


def test_allowed_header_uses_still_exist_and_do_not_touch_the_request():
    """허용 목록이 낡지 않았고, 그 자리에서 읽는 것이 `request`가 아니다."""
    for rel, func in sorted(NOT_REQUEST_HEADERS):
        source = (ROOT / rel).read_text("utf-8")
        uses = [u for u in request_attr_uses(source) if u[0] == func]
        assert uses, (rel, func)
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
                assert isinstance(root, ast.Name) and root.id not in ("request", "req"), (rel, func)


def test_guard_server_reads_headers_only_for_auth_and_length():
    source = (ROOT / "guardrail/src/guardrail/server.py").read_text("utf-8")
    uses = request_attr_uses(source)
    assert {attr for _, attr, _ in uses} == {"headers"}
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
    """검사기 자체 확인: 읽기는 잡고, 요청을 만드는 dict 키는 잡지 않는다."""
    assert session_id_reads('x = body["session_id"]\n') == [1]
    assert session_id_reads('x = body.get("session_id")\n') == [1]
    assert session_id_reads("x = req.session_id\n") == [1]
    assert session_id_reads("def f(session_id):\n    return 1\n") == [1]
    assert session_id_reads('body = {"session_id": s, "messages": m}\n') == []
    assert session_id_reads('"""session_id는 보지 않는다."""\n') == []
    assert request_attr_uses("def f(request):\n    return request.headers.get('x')\n") == [
        ("f", "headers", 2)
    ]
    assert request_attr_uses("async def g(r):\n    return r.cookies, r.client\n") == [
        ("g", "client", 2),
        ("g", "cookies", 2),
    ]


def test_no_print_in_integration_or_app():
    for path in [ROOT / "app.py", *sorted((ROOT / "integration").rglob("*.py"))]:
        tree = ast.parse(path.read_text("utf-8"))
        calls = [
            n.lineno
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print"
        ]
        assert calls == [], _rel(path)
