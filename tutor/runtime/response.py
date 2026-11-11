"""튜터 응답 계약(TU-01, SPEC §4)과 기본 응답(TU-12).

형식 기준은 tutor/contract/tutor.schema.json이다. 채점기 parse_tutor가 받는 모양만 낸다.
"""

import re
from typing import Any, Final

ACTIONS: Final = ("answer", "scaffold", "clarify", "abstain", "redirect", "refuse", "escalate")
ANSWER_MAX_CHARS: Final = 4000  # 유니코드 문자 수(SPEC §4)
# KB 섹션 ID(kb/<과목>/<ID>.md 파일 이름). 계약 스키마의 citations 패턴과 같다(테스트가 맞춰 본다).
# CSV(-table<n>)·HTML(FIN-01)·지어낸 문자열은 응답에 내지 않는다.
CITATION_PATTERN: Final = "^(PY|PR|FIN)-[0-9]{2}-[0-9]{2}$"
_CITATION_RE: Final = re.compile(CITATION_PATTERN)

# TU-12 잠정 기본값: 요청을 못 읽었거나 답을 못 만들었을 때. 문항과 무관한 일반 문장이고
# 전화번호·기관명은 넣지 않는다. 실패 때의 기본 action은 측정 뒤 fallback_action으로 정한다.
CLARIFY_TEXT: Final = (
    "어떤 부분이 궁금한지 조금 더 자세히 알려 줄래요? "
    "질문을 구체적으로 적어 주면 수업 자료에서 찾아 도와줄게요."
)
# 가드 create_app(defaults={"/tutor": ...})에 넘기는 값. 고치지 않는다(응답은 default_response()).
TUTOR_DEFAULT: Final[dict[str, Any]] = {
    "answer": CLARIFY_TEXT,
    "action": "clarify",
    "citations": [],
}


def default_response() -> dict[str, Any]:
    """TUTOR_DEFAULT의 새 복사본. 공유 상수를 건드리지 않게 응답마다 새로 만든다."""
    return {**TUTOR_DEFAULT, "citations": []}


def _encodable(text: str) -> str:
    """짝 없는 surrogate처럼 UTF-8로 쓸 수 없는 문자를 '?'로 바꾼다(JSON 응답이 깨지지 않게)."""
    return text.encode("utf-8", "replace").decode("utf-8")


def finalize(resp: Any) -> dict[str, Any]:
    """어떤 값이 와도 규격 응답 하나로 만든다. 모양이 틀리면 기본값이다.

    answer는 4,000자로 자르고, citations는 KB 섹션 ID 모양만 남기며, 규격에 없는 필드는 버린다.
    """
    if not isinstance(resp, dict):
        return default_response()
    answer, action, citations = resp.get("answer"), resp.get("action"), resp.get("citations")
    if not isinstance(answer, str) or action not in ACTIONS or not isinstance(citations, list):
        return default_response()
    return {
        "answer": _encodable(answer)[:ANSWER_MAX_CHARS],
        "action": action,
        "citations": [c for c in citations if isinstance(c, str) and _CITATION_RE.fullmatch(c)],
    }
