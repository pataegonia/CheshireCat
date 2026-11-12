"""I1.7 E2E 제출 가능성 검사: 지금 저장소를 런박스에 띄우면 제출이 되는가.

루트 app.py가 만든 실제 app에 채점기와 같은 모양의 요청을 보낸다(가짜 LLM, 네트워크 0).
튜터 · 가드 PR이 조립을 깨면 여기서 걸린다. v2(기본 설정)와 baseline 두 모드로 돈다.

- 공개 세트 400행 전부 → 200 + JSON + 경로별 스키마
- hypothesis 퍼징: 임의 바이트, 임의 JSON, 값이 이상한 messages → 200 + 스키마

인증(401), 끝 슬래시(3xx), Content-Type, 예외 → 기본값, 로그 7칸은 test_app.py가, 캐시 · 상태
없음은 test_rules.py가 본다. 여기서 다시 만들지 않는다.
"""

import json
import secrets
from typing import Any

from hypothesis import HealthCheck, given, settings

from tests.integration import strategies
from tests.integration.applib import (
    ANSWER_WORD,
    AUTH,
    ROOT,
    as_bytes,
    assert_guard_contract,
    assert_tutor_contract,
    cache_sizes,
    find_text,
    state_roots,
)

PUBLIC = ROOT / "competition" / "public"
# 채점기(competition/harness/score_local.py의 call)가 보내는 헤더
HEADERS = {**AUTH, "Content-Type": "application/json"}


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


def guard_has_candidate(raw: bytes) -> bool:
    """`/guard` 응답에 output이 있어야 하는지(가드 GR-09와 같은 규칙).

    JSON 객체로 읽히면 최상위에 `candidate_answer` 키가 있는지(값은 무엇이든), 읽히지 않으면
    바이트에 그 키가 보이는지로 정한다.
    """
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        obj = None
    if isinstance(obj, dict):
        return "candidate_answer" in obj
    return b'"candidate_answer"' in raw


def post(client, path: str, raw: bytes):
    return client.post(path, content=raw, headers=HEADERS, follow_redirects=False)


def check(client, path: str, raw: bytes) -> dict[str, Any]:
    """요청 하나: 200, 본문은 JSON 객체, 그 경로의 스키마와 채점기 해석을 통과한다."""
    response = post(client, path, raw)
    assert response.status_code == 200, (path, response.status_code)
    assert response.headers["content-type"].split(";")[0] == "application/json"
    body = response.json()
    if path == "/tutor":
        assert_tutor_contract(body)
    else:
        assert_guard_contract(body, has_candidate=guard_has_candidate(raw))
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


def test_all_400_public_rows_get_200_in_contract(llm_client, llm_mode, fake_send):
    """채점기가 보내는 400개 요청 전부: 200 + JSON + 스키마. 3xx · 4xx · 5xx가 하나도 없다."""
    app = llm_client.app
    probes = _state_probes()
    kept_before = sorted(find_text(state_roots(app), probes))
    caches_before = cache_sizes()

    statuses: dict[int, int] = {}
    from_llm = 0
    for path, rows in (("/tutor", TUTOR_ROWS), ("/guard", GUARD_ROWS)):
        for row in rows:
            raw = as_bytes(scorer_payload(row))
            response = post(llm_client, path, raw)
            statuses[response.status_code] = statuses.get(response.status_code, 0) + 1
            assert response.status_code == 200, (row["id"], response.status_code)
            body = response.json()
            if path == "/tutor":
                assert_tutor_contract(body)
                from_llm += body["answer"].startswith(ANSWER_WORD)
            else:
                assert_guard_contract(body, has_candidate=bool(row.get("candidate_answer")))
    assert statuses == {200: 400}

    # LLM 호출 수. 가드(지금은 상수 판정기)는 부르지 않고, 튜터는 요청당 많아야 1회다.
    calls = fake_send.count
    if llm_mode == "baseline":
        assert calls == len(TUTOR_ROWS)  # 행마다 1회
    else:
        # v2는 LLM 없이 고정 문장으로 끝나는 행이 있다(결정적 신호 · 저신호 직접 판정, TU-05).
        # 세어 본 값: 200행 중 192회.
        assert 0 < calls <= len(TUTOR_ROWS)
    assert 0 < from_llm <= calls  # 가짜 LLM의 답이 실제로 응답까지 나갔다
    assert dict(app.state.tutor_llm.blocked) == {}

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
# 클라이언트(앱)는 테스트 함수마다 하나를 열어 예시 전체가 같이 쓴다. 서버는 요청 사이에 상태가
# 없어야 하므로 예시마다 새로 열 이유가 없고, 같은 앱에 이어 보내는 쪽이 채점 상황에 가깝다.
# 그래서 function_scoped_fixture 검사를 끈다. derandomize: CI에서 실행마다 같은 예시를 쓴다.

FUZZ = settings(
    max_examples=200,
    deadline=None,
    database=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


@FUZZ
@given(raw=strategies.random_bytes)
def test_random_bytes_get_200_in_contract_on_both_paths(llm_client, raw):
    check(llm_client, "/tutor", raw)
    check(llm_client, "/guard", raw)


@FUZZ
@given(raw=strategies.json_bodies)
def test_random_json_gets_200_in_contract_on_both_paths(llm_client, raw):
    check(llm_client, "/tutor", raw)
    check(llm_client, "/guard", raw)


@FUZZ
@given(raw=strategies.odd_message_bodies)
def test_odd_messages_get_200_in_contract_on_both_paths(llm_client, raw):
    check(llm_client, "/tutor", raw)
    check(llm_client, "/guard", raw)


def test_fuzz_strategies_produce_the_intended_shapes():
    """전략이 의도한 입력을 실제로 만든다(빈 입력만 돌고 통과하는 것이 아님을 확인)."""
    seen = {"surrogate": 0, "control": 0, "long": 0, "role_not_str": 0, "content_not_str": 0}

    @settings(max_examples=200, deadline=None, database=None, derandomize=True)
    @given(raw=strategies.odd_message_bodies)
    def collect(raw):
        text = raw.decode("ascii")
        body = json.loads(raw)
        assert isinstance(body["messages"], list)
        seen["surrogate"] += "\\ud8" in text or "\\udc" in text
        seen["control"] += "\\u0000" in text or "\\u001" in text
        seen["long"] += len(raw) > 3_000
        messages = [m for m in body["messages"] if isinstance(m, dict)]
        seen["role_not_str"] += any(
            "role" in m and not isinstance(m["role"], str) for m in messages
        )
        seen["content_not_str"] += any(
            "content" in m and not isinstance(m["content"], str) for m in messages
        )

    collect()
    assert all(count > 0 for count in seen.values()), seen
