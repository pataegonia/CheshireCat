"""요청 해석(TU-01·TU-02): 예외 없이 읽을 수 있는 만큼 읽는다.

규격(SPEC §3): {"spec_version", "session_id", "messages": [{"role", "content"}]}.
spec_version·session_id는 보지 않는다. 동작은 messages로만 정한다(TU-18).
"""

import json
from dataclasses import dataclass
from typing import Any

WINDOW = 6  # 첫 학습자 발화 + 마지막 메시지 6개(TU-02, 가드 GR-04와 같은 창)


@dataclass(frozen=True)
class Turn:
    role: str  # "user"·"assistant"가 아니어도 그대로 둔다(문자열이 아니면 "")
    content: str


@dataclass(frozen=True)
class TutorRequest:
    turns: tuple[Turn, ...]

    @property
    def last_user(self) -> str | None:
        """판단 대상인 마지막 학습자 발화(TU-02). 역할 순서가 어긋나도 마지막 user를 고른다."""
        return next((t.content for t in reversed(self.turns) if t.role == "user"), None)

    def window(self) -> tuple[Turn, ...]:
        """첫 학습자 발화 + 마지막 메시지 WINDOW개 + 판단 대상(마지막 학습자 발화).

        판단 대상은 뒤에 튜터 발화가 WINDOW개 넘게 붙어도(규격 밖 입력) 넣는다(#97 tidy와 같은
        규칙). 버튼 문구 빼기는 #97 tidy(TU-02)가 맡는다.
        """
        users = [i for i, t in enumerate(self.turns) if t.role == "user"]
        keep = set(range(max(len(self.turns) - WINDOW, 0), len(self.turns)))
        keep |= set(users[:1]) | set(users[-1:])
        return tuple(t for i, t in enumerate(self.turns) if i in keep)

    def search_query(self) -> tuple[str, list[str]]:
        """검색 질의: (창 안의 마지막 학습자 발화, 그 앞 학습자 발화들).

        학습자 발화가 없으면 빈 질의다.
        """
        texts = [t.content for t in self.window() if t.role == "user"]
        return (texts[-1], texts[:-1]) if texts else ("", [])


def load_json(raw: bytes) -> Any:
    """JSON 값 하나. JSON이 아니거나, 인코딩이 깨졌거나, 너무 깊거나 큰 수면 None."""
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):  # UnicodeDecodeError·JSONDecodeError는 ValueError
        return None


def parse_request(body: Any) -> TutorRequest | None:
    """messages에서 content가 문자열인 항목만 읽는다. 읽을 것이 없으면 None(→ TU-12 기본값)."""
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        return None
    turns = tuple(
        Turn(m["role"] if isinstance(m.get("role"), str) else "", m["content"])
        for m in body["messages"]
        if isinstance(m, dict) and isinstance(m.get("content"), str)
    )
    return TutorRequest(turns) if turns else None
