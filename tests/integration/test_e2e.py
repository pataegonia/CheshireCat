"""I1.7 E2E 제출 가능성 검사: 지금 저장소를 런박스에 띄우면 제출이 되는가.

루트 app.py가 만든 실제 app에 채점기와 같은 모양의 요청을 보낸다(가짜 LLM, 네트워크 0).
튜터 · 가드 PR이 조립을 깨면 여기서 걸린다. `/tutor`는 v2(기준 설정, 동결 토글 끔)와 baseline 두
모드로 돈다.

- 공개 세트 400행 전부 → 200 + JSON + 경로별 스키마. 가짜 LLM은 "다양한 답" 모드라 응답이
  여러 모양(action 7종, 인용 0–2개, 4,000자 초과 답, 빈 답)을 지난다. v2는 가짜가 틀린 문구로만
  인용한 섹션이 응답 citations에 남지 않는지도 본다(근거 검증, IN-04).
- hypothesis 퍼징: 임의 바이트, 임의 JSON, 값이 이상한 messages, 유효한 대화 + 이상 값
- 고정 경계 사례: 4MiB 앞뒤, 깊은 중첩, messages 1,000개, 비-UTF8, 최상위 배열

가짜 LLM은 튜터 쪽에만 있다. 가드는 지금 LLM이 없는 상수 판정기라 `/guard`는 조립 · 형식만
본다. 가드가 실제 판정기로 바뀌면 가드 LLM 가짜가 따로 필요하다.

인증(401), 끝 슬래시(3xx), Content-Type, 예외 → 기본값, 로그 7칸은 test_app.py가, 캐시 · 상태
없음은 test_rules.py가 본다. 여기서 다시 만들지 않는다.
"""

import collections
import json
import secrets
from typing import Any

import jsonschema
import pytest
from hypothesis import HealthCheck, given, settings

from integration.actions import SECTION_IDS
from tests.integration import strategies
from tests.integration.applib import (
    ANSWER_WORD,
    AUTH,
    GUARD_BUDGET_HINT,
    GUARD_RESPONSE_SCHEMA,
    MAX_LLM_CALLS_PER_REQUEST,
    ROOT,
    EvidenceLog,
    as_bytes,
    assert_tutor_contract,
    cache_sizes,
    find_text,
    guard_call_budget,
    guard_calls_per_request,
    scoring,
    state_roots,
    user,
)

PUBLIC = ROOT / "competition" / "public"
# 채점기(competition/harness/score_local.py의 call)가 보내는 헤더
HEADERS = {**AUTH, "Content-Type": "application/json"}
ANSWER_MAX_CHARS = 4_000
MIB = 1024 * 1024
NESTING_DEPTH = 1_000_000  # 깊은 중첩 경계 사례의 깊이
# 퍼징 전략 하나 · 모드 하나에 돌리는 예시 수. test_e2e.py 전체가 개발 PC에서 25초 안에 들게
# 정했다(CI 러너는 2코어라 약 2배 걸린다. 수용 기준은 60초).
FUZZ_EXAMPLES = 80


def _rows(endpoint: str) -> list[dict[str, Any]]:
    text = (PUBLIC / f"{endpoint}.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


TUTOR_ROWS = _rows("tutor")
GUARD_ROWS = _rows("guard")


def scorer_payload(row: dict[str, Any]) -> dict[str, Any]:
    """`score_local.score_local.one`이 만드는 요청 본문과 같은 모양.

    `candidate_answer`는 값이 있을 때만 넣고, `session_id`는 요청마다 새 난수다.
    """
    payload = {
        "spec_version": "1.0",
        "session_id": "s_" + secrets.token_hex(8),
        "messages": row["messages"],
    }
    if row.get("candidate_answer"):
        payload["candidate_answer"] = row["candidate_answer"]
    return payload


def has_candidate_key(raw: bytes) -> bool:
    """요청에 후보 답변이 있는가: JSON 객체의 최상위에 `candidate_answer` 키가 있다."""
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        return False
    return isinstance(obj, dict) and "candidate_answer" in obj


def assert_guard_response(body: Any, *, has_candidate: bool) -> None:
    """SPEC 기준: 후보가 있으면 output이 필수다. 없으면 output이 있어도 되고(채점기가 무시한다),
    있으면 모양이 맞아야 한다. 가드는 본문을 못 읽었을 때 output을 붙인 기본 응답을 낸다(GR-09).

    "후보가 없으면 output도 없다"는 엄격한 검사는 test_app.py가 한다.
    """
    jsonschema.validate(body, GUARD_RESPONSE_SCHEMA)
    assert scoring.parse_guard(body, has_candidate) is not None
    if has_candidate:
        assert "output" in body
    if "output" in body:
        assert scoring.parse_guard(body, True) is not None  # 붙어 있는 output은 유효하다


def assert_tutor_response(body: Any) -> None:
    """스키마 + 채점기 해석 + 규격 값: 4,000자 이하, 실제로 있는 섹션 ID, 중복 없음."""
    assert_tutor_contract(body)
    assert len(body["answer"]) <= ANSWER_MAX_CHARS
    citations = body["citations"]
    assert all(sid in SECTION_IDS for sid in citations), citations
    assert len(set(citations)) == len(citations), citations


def wrong_quote_sids(body: Any, logs: list[EvidenceLog]) -> tuple[int, list[str]]:
    """가짜 LLM이 틀린 문구로만 인용한 섹션 중 (검사한 수, 응답 citations에 남은 것).

    근거 검증(`integration.actions._verified_sids`)은 문구가 원문에 없는 evidence를 버린다.
    검색 1위 섹션은 세지 않는다: 인용 규칙이 always이고 검증된 근거가 없으면 검색 1위로
    채우므로(`_rank_citations`의 fill) 검증과 무관하게 들어갈 수 있다.
    """
    checked = [sid for log in logs for sid in sorted(log.wrong_only_sids()) if sid != log.first_sid]
    return len(checked), [sid for sid in checked if sid in body["citations"]]


def post(client, path: str, raw: bytes):
    return client.post(path, content=raw, headers=HEADERS, follow_redirects=False)


def check(client, path: str, raw: bytes) -> dict[str, Any]:
    """요청 하나: 200, 본문은 JSON 객체, 그 경로의 스키마와 채점기 해석을 통과한다."""
    response = post(client, path, raw)
    assert response.status_code == 200, (path, response.status_code)
    assert response.headers["content-type"].split(";")[0] == "application/json"
    body = response.json()
    if path == "/tutor":
        assert_tutor_response(body)
    else:
        assert_guard_response(body, has_candidate=has_candidate_key(raw))
    return body


# ── 공개 세트 400행 ───────────────────────────────────────────────────────


def test_public_set_rows_are_what_the_scorer_sends():
    assert (len(TUTOR_ROWS), len(GUARD_ROWS)) == (200, 200)
    assert {row["endpoint"] for row in TUTOR_ROWS} == {"tutor"}
    assert {row["endpoint"] for row in GUARD_ROWS} == {"guard"}
    with_candidate = [row for row in GUARD_ROWS if row.get("candidate_answer")]
    assert len(with_candidate) == 86  # 후보 답변이 있는 행과 없는 행(114)이 둘 다 있다
    assert not any("candidate_answer" in row for row in TUTOR_ROWS)
    payload = scorer_payload(with_candidate[0])
    assert sorted(payload) == ["candidate_answer", "messages", "session_id", "spec_version"]
    assert sorted(scorer_payload(TUTOR_ROWS[0])) == ["messages", "session_id", "spec_version"]
    assert len(SECTION_IDS) == 48


def test_all_400_public_rows_get_200_in_contract(llm_client, llm_mode, fake_send, fake_guard_send):
    """채점기가 보내는 400개 요청 전부: 200 + JSON + 스키마. 3xx · 4xx · 5xx가 하나도 없다."""
    fake_send.vary = True  # LLM 답의 모양을 프롬프트 해시로 바꿔 가며 낸다
    fake_guard_send.vary = True  # 가드 nano의 판정도(가드가 nano를 부르게 되면 범주 10종을 지난다)
    app = llm_client.app
    probes = _state_probes()
    kept_before = sorted(find_text(state_roots(app), probes))
    caches_before = cache_sizes()

    actions: collections.Counter[str] = collections.Counter()
    cited: collections.Counter[int] = collections.Counter()
    from_llm = 0
    wrong_checked = 0
    for path, rows in (("/tutor", TUTOR_ROWS), ("/guard", GUARD_ROWS)):
        for row in rows:
            raw = as_bytes(scorer_payload(row))
            calls_before = len(fake_send.evidence)
            guard_before = fake_guard_send.count
            response = post(llm_client, path, raw)
            assert response.status_code == 200, (row["id"], response.status_code)
            # 가드 nano 호출은 요청마다 그 경로의 상한 안이다(합계만 보면 서로 상쇄된다)
            guard_used = fake_guard_send.count - guard_before
            assert guard_used <= guard_calls_per_request(path), GUARD_BUDGET_HINT
            body = response.json()
            if path == "/tutor":
                assert_tutor_response(body)
                actions[body["action"]] += 1
                cited[len(body["citations"])] += 1
                from_llm += ANSWER_WORD in body["answer"]
                # 이 요청에서 가짜 LLM이 틀린 문구로만 인용한 섹션은 응답에 남지 않는다
                checked, left = wrong_quote_sids(body, fake_send.evidence[calls_before:])
                assert left == [], (row["id"], left, body["citations"])
                wrong_checked += checked
            else:
                assert_guard_response(body, has_candidate=has_candidate_key(raw))
    assert len(fake_send.evidence) == fake_send.count

    # 응답이 실제로 여러 모양을 지났다(가짜 LLM이 한 가지 답만 내면 후처리 대부분이 돌지 않는다).
    # 세어 본 분포(200행):
    #   v2       clarify 36 · refuse 33 · abstain 30 · escalate 30 · answer 27 · scaffold 25 ·
    #            redirect 19 / 인용 0개 150 · 1개 50(v2는 max_citations 1, cite_top1 켜기 전 집계.
    #            켠 뒤로는 검색 1위가 더해진 2개도 나온다)
    #   baseline refuse 40 · scaffold 39 · answer 30 · escalate 27 · redirect 24 · clarify 22 ·
    #            abstain 18 / 인용 0개 64 · 1개 67 · 2개 69
    assert len(actions) >= 5, actions
    assert cited[0] > 0 and sum(n for count, n in cited.items() if count >= 1) > 0, cited
    if llm_mode == "baseline":
        # 프롬프트의 섹션 표기(`[PY-01-01]`)가 바뀌면 가짜 인용이 조용히 []가 된다. 그것을 막는다
        assert cited[1] > 0 and cited[2] > 0, cited
    assert from_llm > 0  # 가짜 LLM의 답이 실제로 응답까지 나갔다
    # 틀린 인용 검사가 실제로 돌았다. 근거 검증은 v2에만 있다: baseline은 LLM이 문구 없이 섹션 ID만
    # 내고(`BaselineOut.citations`) 근거 묶음 안의 ID인지만 본다. 세어 본 값: v2 74개, baseline 0개
    assert (wrong_checked > 0) == (llm_mode == "v2"), wrong_checked

    # LLM 호출 수. 가드(지금은 상수 판정기)는 부르지 않는다. v2는 LLM 없이 고정 문장으로 끝나는
    # 행이 있다(결정적 신호 · 저신호 직접 판정, TU-05). 세어 본 값: v2 192회, baseline 200회.
    limit = len(TUTOR_ROWS) * MAX_LLM_CALLS_PER_REQUEST[llm_mode]
    # 하한: 튜터 200행 중 180행 이상이 LLM까지 간다(입구에서 고정 문장으로 끝나는 행이 늘면 걸린다)
    assert len(TUTOR_ROWS) * 9 // 10 <= fake_send.count <= limit
    if llm_mode == "baseline":
        assert fake_send.count == limit  # 행마다 빠짐없이 부른다
    assert dict(app.state.tutor_llm.blocked) == {}
    # 가드 nano 호출 수(가드 LLM의 가짜 전송이 센다): 요청마다(위 반복)와 합계 모두 상한 안이다
    guard_limit = guard_call_budget(tutor=len(TUTOR_ROWS), guard=len(GUARD_ROWS))
    assert fake_guard_send.count <= guard_limit, GUARD_BUDGET_HINT

    # 400행을 보낸 뒤에도 요청 내용이 남지 않는다. 기동 때 읽은 자료(공개 세트 예시 등)에 같은
    # 문장이 이미 있을 수 있어, 보내기 전과 같은지로 본다.
    assert sorted(find_text(state_roots(app), probes)) == kept_before
    assert cache_sizes() == caches_before


def _state_probes() -> tuple[str, ...]:
    """상태 탐색에 쓸 표본: 마지막 발화(20자 이상인 것 중 처음 · 가운데 · 끝), 후보 답변 2개,
    가짜 답변 표식. 짧은 발화("응" 등)는 다른 문장에 우연히 들어 있을 수 있어 뺀다."""
    probes = [ANSWER_WORD + " "]
    for rows in (TUTOR_ROWS, GUARD_ROWS):
        long_enough = [c for row in rows if len(c := row["messages"][-1]["content"]) >= 20]
        probes += [long_enough[0], long_enough[len(long_enough) // 2], long_enough[-1]]
    probes += [row["candidate_answer"] for row in GUARD_ROWS if row.get("candidate_answer")][:2]
    assert len(probes) == 9
    return tuple(probes)


# ── hypothesis 퍼징 ───────────────────────────────────────────────────────
# @given은 테스트 함수 안의 함수에 붙인다. 앱(클라이언트)은 pytest 픽스처로 테스트마다 하나를
# 열고 예시 전체가 같이 쓴다: 서버는 요청 사이에 상태가 없어야 하므로 예시마다 새로 열 이유가
# 없고, 같은 앱에 이어 보내는 쪽이 채점 상황에 가깝다. 이렇게 하면 hypothesis의
# function_scoped_fixture 검사에 걸리지 않고, 보낸 예시 수와 LLM 호출 수를 같이 셀 수 있다.
# derandomize: 실행마다 같은 예시를 쓴다(CI에서 흔들리지 않게).

FUZZ = {
    "random_bytes": strategies.random_bytes,
    "json_bodies": strategies.json_bodies,
    "odd_messages": strategies.odd_message_bodies,
    "conversations": strategies.conversation_bodies,
}


def fuzz(
    strategy, client, path: str, examples: int = FUZZ_EXAMPLES, *, guard_send=None
) -> list[int]:
    """전략의 예시를 `path`에 보내 하나씩 검사한다. 보낸 본문의 길이 목록을 돌려준다.

    `guard_send`(가드 LLM의 가짜 전송)를 주면 예시마다 가드 nano 호출 수가 그 경로의 상한 안인지도
    본다. 예시는 하나씩 차례로 보내므로 앞뒤 차이가 그 요청 하나의 호출 수다.
    """
    sent: list[int] = []

    @settings(
        max_examples=examples,
        deadline=None,
        database=None,
        derandomize=True,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    )
    @given(raw=strategy)
    def run(raw: bytes) -> None:
        sent.append(len(raw))
        before = guard_send.count if guard_send is not None else 0
        check(client, path, raw)
        if guard_send is not None:
            used = guard_send.count - before
            assert used <= guard_calls_per_request(path), GUARD_BUDGET_HINT

    run()
    assert len(sent) >= examples
    return sent


@pytest.mark.parametrize("name", ["random_bytes", "json_bodies", "odd_messages"])
def test_fuzzed_bodies_get_200_in_contract_on_tutor(llm_client, fake_send, name):
    fake_send.vary = True
    fuzz(FUZZ[name], llm_client, "/tutor")


def test_fuzzed_conversations_reach_the_llm_on_tutor(llm_client, llm_mode, fake_send):
    """유효한 대화 + 이상 값: 입구에서 끝나지 않고 검색 · LLM · 후처리까지 간다."""
    fake_send.vary = True
    sent = fuzz(strategies.conversation_bodies, llm_client, "/tutor")
    # 예시의 95% 이상이 튜터 LLM까지 갔다. 세어 본 값: v2 80/80, baseline 80/80
    assert fake_send.count * 20 >= len(sent) * 19, (fake_send.count, len(sent))
    assert fake_send.count <= len(sent) * MAX_LLM_CALLS_PER_REQUEST[llm_mode]


@pytest.mark.parametrize("name", sorted(FUZZ))
def test_fuzzed_bodies_get_200_in_contract_on_guard(client, fake_send, fake_guard_send, name):
    """`/guard`는 튜터 모드와 무관하다(튜터 설정만 다르다). 기준 설정(동결 토글 끔)으로 한 번만
    돈다."""
    fake_guard_send.vary = True
    sent = fuzz(FUZZ[name], client, "/guard", guard_send=fake_guard_send)  # 예시마다 상한도 본다
    assert fake_send.count == 0  # 가드는 튜터 LLM을 부르지 않는다
    assert fake_guard_send.count <= guard_call_budget(guard=len(sent)), GUARD_BUDGET_HINT


# ── 고정 경계 사례 ────────────────────────────────────────────────────────


def _padded(size: int) -> bytes:
    """정상 요청 뒤에 공백을 붙여 정확히 `size`바이트로 만든 JSON(해석 결과는 정상 요청이다)."""
    raw = as_bytes(
        {**user("리스트에 값을 넣는 방법이 뭐야?"), "candidate_answer": "append를 써요."}
    )
    return raw + b" " * (size - len(raw))


def _turns(count: int) -> bytes:
    messages = [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"{i}번째 말이에요. 리스트가 뭐야?",
        }
        for i in range(count)
    ]
    return as_bytes({"spec_version": "1.0", "session_id": "s_x", "messages": messages})


_NON_UTF8 = (
    b'{"messages": [{"role": "user", "content": "\xff\xfe list \xc0"}], "candidate_answer": "\xff"}'
)
EDGE_BODIES = {
    "just_under_4mib": lambda: _padded(4 * MIB - 1),  # 본문 상한(4MiB) 안: 정상 처리
    "4mib_plus_1": lambda: _padded(4 * MIB + 1),  # 상한 초과: 기본 응답
    # 깊이 1,000,000: Python 3.12–3.14의 json.loads가 RecursionError를 낸다(3.14는 깊이 10,000을
    # 예외 없이 읽는다). 2MB라 본문 상한 4MiB 안이다.
    "nested_1000000_arrays": lambda: b"[" * NESTING_DEPTH + b"]" * NESTING_DEPTH,
    "1000_messages": lambda: _turns(1_000),  # 마지막은 assistant
    "1001_messages": lambda: _turns(1_001),  # 마지막은 user
    "non_utf8_in_string": lambda: _NON_UTF8,
    "top_level_array": lambda: as_bytes([user("리스트에 값을 넣는 방법이 뭐야?")]),
}


def test_edge_bodies_have_the_intended_sizes():
    assert len(EDGE_BODIES["just_under_4mib"]()) == 4 * MIB - 1
    assert len(EDGE_BODIES["4mib_plus_1"]()) == 4 * MIB + 1
    assert json.loads(EDGE_BODIES["just_under_4mib"]())["messages"][0]["role"] == "user"
    assert len(json.loads(EDGE_BODIES["1000_messages"]())["messages"]) == 1_000
    nested = EDGE_BODIES["nested_1000000_arrays"]()
    assert len(nested) == 2 * NESTING_DEPTH < 4 * MIB  # 크기 상한이 아니라 깊이에서 걸린다
    with pytest.raises(RecursionError):
        json.loads(nested)
    with pytest.raises(UnicodeDecodeError):
        json.loads(EDGE_BODIES["non_utf8_in_string"]())


@pytest.mark.parametrize("name", sorted(EDGE_BODIES))
def test_edge_body_gets_200_in_contract_on_tutor(llm_client, fake_send, name):
    fake_send.vary = True
    raw = EDGE_BODIES[name]()
    body = check(llm_client, "/tutor", raw)
    if name in ("just_under_4mib", "1001_messages"):
        assert fake_send.count == 1, name  # 상한 안의 정상 요청은 LLM까지 간다
    if name == "4mib_plus_1":
        assert fake_send.count == 0 and body["action"] == "clarify"
    # 큰 본문 · 깨진 본문 뒤에도 같은 앱이 다음 요청에 답한다
    check(llm_client, "/tutor", as_bytes(user("예산을 세울 때 먼저 할 일이 뭐야?")))


@pytest.mark.parametrize("name", sorted(EDGE_BODIES))
def test_edge_body_gets_200_in_contract_on_guard(client, name):
    raw = EDGE_BODIES[name]()
    body = check(client, "/guard", raw)
    if name == "just_under_4mib":
        assert "output" in body  # 상한 안이면 후보 답변을 읽는다
    check(client, "/guard", as_bytes(user("예산을 세울 때 먼저 할 일이 뭐야?")))


# ── 전략 자체 확인 ────────────────────────────────────────────────────────


def _lone_surrogates(text: str) -> int:
    """문자열 안의 U+D800–DFFF 수. json.loads를 지난 문자열에서는 짝이 맞는 것은 한 글자로
    합쳐지므로 남은 것은 모두 짝 없는 surrogate다(이모지는 세지 않는다)."""
    return sum(0xD800 <= ord(ch) <= 0xDFFF for ch in text)


def _examples(strategy, count: int = 120) -> list[bytes]:
    seen: list[bytes] = []

    @settings(max_examples=count, deadline=None, database=None, derandomize=True)
    @given(raw=strategy)
    def collect(raw: bytes) -> None:
        seen.append(raw)

    collect()
    return seen


def test_odd_message_strategy_produces_the_intended_shapes():
    """전략이 의도한 입력을 실제로 만든다(빈 입력만 돌고 통과하는 것이 아님을 확인)."""
    seen: collections.Counter[str] = collections.Counter()
    for raw in _examples(strategies.odd_message_bodies):
        body = json.loads(raw)
        assert isinstance(body["messages"], list)
        messages = [m for m in body["messages"] if isinstance(m, dict)]
        contents = [m["content"] for m in messages if isinstance(m.get("content"), str)]
        seen["surrogate_in_content"] += any(_lone_surrogates(c) for c in contents)
        seen["control_in_content"] += any(ch < " " for c in contents for ch in c)
        seen["long_content"] += any(len(c) >= 3_000 for c in contents)
        seen["role_not_str"] += any(
            "role" in m and not isinstance(m["role"], str) for m in messages
        )
        seen["content_not_str"] += any(
            "content" in m and not isinstance(m["content"], str) for m in messages
        )
    assert _lone_surrogates("😀") == 0 and _lone_surrogates(json.loads('"\\ud83d"')) == 1
    for name in (
        "surrogate_in_content",
        "control_in_content",
        "long_content",
        "role_not_str",
        "content_not_str",
    ):
        assert seen[name] > 0, (name, seen)


def test_conversation_strategy_produces_valid_conversations_with_odd_values():
    seen: collections.Counter[str] = collections.Counter()
    turns: collections.Counter[int] = collections.Counter()
    examples = _examples(strategies.conversation_bodies)
    for raw in examples:
        body = json.loads(raw)
        messages = body["messages"]
        assert 1 <= len(messages) <= 12
        assert all(m["role"] in ("user", "assistant") for m in messages)
        assert all(isinstance(m["content"], str) and m["content"] for m in messages)
        turns[len(messages)] += 1
        contents = "".join(m["content"] for m in messages)
        seen["last_user"] += messages[-1]["role"] == "user"
        seen["last_assistant"] += messages[-1]["role"] == "assistant"
        seen["both_roles"] += len({m["role"] for m in messages}) == 2
        seen["surrogate"] += _lone_surrogates(contents) > 0
        seen["control"] += any(ch in contents for ch in "\x00\x07\x1b")
        seen["zero_width"] += "​" in contents
        seen["emoji"] += "😀" in contents
        seen["long"] += len(contents) >= 20_000
        seen["code_block"] += "```" in contents
        seen["extra_key"] += any(set(m) - {"role", "content"} for m in messages)
        seen["odd_session"] += body.get("session_id") != "s_x"
        seen["candidate"] += "candidate_answer" in body
    assert len(seen) == 12 and all(seen[name] > 0 for name in seen), seen
    assert seen["last_user"] >= len(examples) * 0.8  # 마지막 메시지는 대부분 user다
    assert turns[1] > 0 and max(turns) >= 10


def test_bytes_and_json_strategies_produce_large_and_message_shaped_bodies():
    sizes = [len(raw) for raw in _examples(strategies.random_bytes)]
    assert max(sizes) >= 300_000 and min(sizes) == 0
    assert sum(size >= 4_096 for size in sizes) >= 12
    with_messages = sum(
        isinstance(body := json.loads(raw), dict) and "messages" in body
        for raw in _examples(strategies.json_bodies)
    )
    assert with_messages >= 24
