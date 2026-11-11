"""TU-02 대화 정리: 판단 대상(마지막 학습자 발화), 창, 버튼 문구 표시, 숨김 문자 처리.

- 요청 본문의 `messages`를 예외 없이 읽는다. 형식이 깨져도(목록이 아님, 원소가 dict가 아님,
  content가 문자열이 아님, 역할 순서가 어긋남) 읽을 수 있는 메시지만 쓴다(SPEC §3, TU-01).
- 판단 대상 = 마지막 `user` 메시지. 이력은 지시어 해소와 검색에만 쓴다(PRD §3).
- 창 = 첫 학습자 발화 + 마지막 메시지 6개(가드 GR-04).
  첫 학습자 발화가 이미 마지막 6개 안에 있으면 중복하지 않는다.
- 정규화본은 가드 `preprocess.normalize`(보이지 않는 문자 제거·전각 접기·자모 조합)를 쓴다.
  태그 문자(U+E0020–E007E)는 ASCII로 풀어 끝에 `[숨긴 문자: …]`로 붙인다(가드 GR-02).
- 길이: 메시지마다 원문 2,000자, 창의 대화 부분 약 4,000자. 넘으면 앞뒤를 남기고 가운데를 줄인다.
- 버튼 문구: 첫 학습자 발화가 "자료·코드·문제를 요약·설명·힌트·도움" 같은 일반 요청만으로 된
  짧은 문장이면 표시한다. 공개 세트 문구 목록이 아니라 일반 어휘로 판별한다(TD-10, TU-18).

요청 사이에 아무것도 남기지 않는다(모듈 전역은 읽기 전용 상수뿐, TU-18).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from guardrail.preprocess import normalize

Role = Literal["user", "assistant", "other"]

WINDOW_LAST = 6  # 창에 넣는 마지막 메시지 수(가드 GR-04와 같음)
MAX_MESSAGE_CHARS = 2000  # 메시지 원문 상한(가드 input.max_chars 기본값과 같음)
MAX_WINDOW_CHARS = 4000  # 창의 대화 부분 상한(정규화본 기준)
MAX_HIDDEN_CHARS = 500  # 풀어 쓴 숨긴 문자 상한
CUT_MARK = " …(중략)… "
MIN_KEEP = 40  # 줄일 때 남기는 최소 글자 수(앞뒤 합)

# --- 태그 문자(숨긴 문자) ---

_TAG_FIRST, _TAG_LAST = 0xE0020, 0xE007E  # 대응 ASCII가 있는 태그 문자
_TAG_ANY = re.compile("[\U000e0000-\U000e007f]")
# 하위 지역 깃발 이모지: 🏴 + 소문자·숫자 태그 1–7개 + 종료 태그(U+E007F).
# 숨긴 텍스트로 세지 않는다(가드 GR-06)
_FLAG_SEQ = re.compile("\U0001f3f4[\U000e0061-\U000e007a\U000e0030-\U000e0039]{1,7}\U000e007f")


def hidden_text(text: str) -> str:
    """깃발 이모지 밖의 태그 문자를 ASCII로 풀어 돌려준다. 없으면 빈 문자열."""
    outside = _FLAG_SEQ.sub("", text)
    return "".join(chr(ord(ch) - 0xE0000) for ch in outside if _TAG_FIRST <= ord(ch) <= _TAG_LAST)


def strip_tags(text: str) -> str:
    """태그 문자만 지운 원문(자모 조합 전). 저신호 직접 판정이 이 뷰를 본다(가드 GR-06)."""
    return _TAG_ANY.sub("", text)


# --- 자르기 ---


def cut_middle(text: str, limit: int) -> str:
    """앞뒤를 남기고 가운데를 줄인다(붙여 넣은 코드의 앞부분과 질문이 있는 끝부분을 남긴다)."""
    if len(text) <= limit:
        return text
    keep = max(limit - len(CUT_MARK), MIN_KEEP)
    head = keep * 2 // 3
    tail = keep - head
    return text[:head] + CUT_MARK + text[len(text) - tail :]


# --- 버튼 문구 ---

MAX_BUTTON_CHARS = 40
# 수업 화면의 일반 대상(특정 예제·과목 내용이 아닌 말)
_GENERIC_OBJECTS = (
    "학습자료", "학습 자료", "수업자료", "수업 자료", "자료", "영상", "강의", "수업", "내용",
    "코드", "문제", "퀴즈", "실습", "과제", "풀이", "의도", "방법", "부분", "개념",
)  # fmt: skip
# 정형 요청의 행위(요약·설명·힌트·도움)
_REQUEST_ACTS = re.compile(r"요약|설명|힌트|도움|도와|알려|가르쳐|알고\s*싶")
# 남아도 되는 말: 지시어·조사·요청 어미·정도 부사(낱말 단위)
_FILLERS = frozenset(
    (
        "이", "그", "이번", "지금", "의", "을", "를", "은", "는", "이가", "가", "에", "에서",
        "좀", "다시", "한번", "해", "해서", "해요", "해줘", "줘", "해주세요", "주세요", "줄래요",
        "줄", "주실", "주시겠어요", "받을", "받고", "수", "있을까요", "있나요", "있어요",
        "싶어요", "싶습니다", "어요", "습니다", "할", "부탁해요", "부탁드려요", "자세히",
        "차근차근", "단계별로", "간단히", "쉽게", "하는", "데", "푸는", "중", "도중", "어려운",
        "어려워요", "모르겠어요", "요",
    )
)  # fmt: skip
_WORD = re.compile(r"[0-9A-Za-z가-힣ㄱ-ㅣ]+")


def _strip_vocab(text: str) -> str:
    rest = _REQUEST_ACTS.sub(" ", text)
    for word in sorted(_GENERIC_OBJECTS, key=len, reverse=True):
        rest = rest.replace(word, " ")
    return rest


def is_button_phrase(text: str) -> bool:
    """짧은 정형 요청(요약·설명·힌트·도움 요청)만으로 된 발화인가.

    일반 대상 말과 요청 행위, 조사·어미를 지우고 남는 말이 없으면 버튼 문구로 본다.
    예제 이름·과목 용어·숫자가 하나라도 남으면 학습자가 직접 쓴 질문이다.
    """
    s = " ".join(text.split())
    if not s or len(s) > MAX_BUTTON_CHARS or not _REQUEST_ACTS.search(s):
        return False
    leftover = [w for w in _WORD.findall(_strip_vocab(s)) if w not in _FILLERS]
    return not leftover


# --- 대화 ---


@dataclass(frozen=True)
class Turn:
    index: int  # 원래 messages 안의 위치
    role: Role
    text: str  # 정규화본(+숨긴 문자 표기), 자른 뒤
    is_target: bool  # 판단 대상(마지막 학습자 발화)
    is_button: bool  # 첫 학습자 발화이면서 버튼 문구


@dataclass(frozen=True)
class Conversation:
    window: tuple[Turn, ...]
    target: str  # 판단 대상 정규화본(+숨긴 문자 표기). 학습자 발화가 없으면 ""
    target_raw: str  # 판단 대상 원문에서 태그 문자만 지운 것(자른 뒤, 자모 조합 전)
    target_hidden: str  # 판단 대상의 숨긴 문자를 푼 것. 없으면 ""
    n_messages: int  # 읽을 수 있었던 메시지 수
    n_user: int  # 그중 학습자 발화 수

    @property
    def has_history(self) -> bool:
        """판단 대상 앞에 대화가 있는가(저신호 직접 판정 조건, TU-05)."""
        return self.n_messages > 1

    @property
    def is_empty(self) -> bool:
        """판단 대상이 없거나 공백뿐인가(1턴이어도 고정 clarify, TU-05)."""
        return not self.target_raw.strip() and not self.target_hidden

    def search_turns(self) -> tuple[str, ...]:
        """검색 질의용 학습자 발화(창 안, 버튼 문구 제외, 판단 대상이 마지막). TU-03."""
        return tuple(t.text for t in self.window if t.role == "user" and not t.is_button)


def _role(raw: Any) -> Role:
    return raw if raw in ("user", "assistant") else "other"


def read_messages(raw: Any) -> list[tuple[Role, str]]:
    """읽을 수 있는 메시지만 (역할, 원문)으로. 예외를 내지 않는다."""
    if not isinstance(raw, list):
        return []
    out: list[tuple[Role, str]] = []
    for m in raw:
        if isinstance(m, dict) and isinstance(m.get("content"), str):
            out.append((_role(m.get("role")), m["content"]))
    return out


# 가드 normalize가 바꾸지 않는 문자만으로 된 글(ASCII + 완성 음절).
# 이런 글은 글자마다 도는 normalize를 건너뛴다
_PLAIN = re.compile("[\x00-\x7f가-힣]*")


def normalize_fast(text: str) -> str:
    """가드 `normalize`와 같은 결과. 바뀔 글자가 없으면 그대로 돌려준다(TU-02 p95 ≤10ms)."""
    return text if _PLAIN.fullmatch(text) else normalize(text)


def _view(raw: str) -> tuple[str, str]:
    """(정규화본 + 숨긴 문자 표기, 풀어 쓴 숨긴 문자)."""
    hidden = cut_middle(hidden_text(raw), MAX_HIDDEN_CHARS)
    text = normalize_fast(raw).strip()
    if hidden:
        text = f"{text} [숨긴 문자: {hidden}]".strip()
    return text, hidden


def _window_indices(messages: Sequence[tuple[Role, str]], first_user: int | None) -> list[int]:
    n = len(messages)
    idx = list(range(max(n - WINDOW_LAST, 0), n))
    if first_user is not None and first_user not in idx:
        idx.insert(0, first_user)
    return idx


def _fit_texts(texts: list[str], keep: int | None) -> list[str]:
    """합계가 창 상한을 넘으면 keep(판단 대상)이 아닌 글을 오래된 것부터 앞뒤만 남기고 줄인다."""
    total = sum(map(len, texts))
    out = list(texts)
    for i, text in enumerate(out):
        if total <= MAX_WINDOW_CHARS:
            break
        if i == keep:
            continue
        new = cut_middle(
            text, max(len(text) - (total - MAX_WINDOW_CHARS), MIN_KEEP + len(CUT_MARK))
        )
        total -= len(text) - len(new)
        out[i] = new
    return out


def tidy(raw_messages: Any) -> Conversation:
    """요청의 `messages`를 정리한다. 어떤 입력에도 예외를 내지 않는다(TU-02 수용 기준)."""
    messages = [
        (role, cut_middle(text, MAX_MESSAGE_CHARS)) for role, text in read_messages(raw_messages)
    ]
    user_idx = [i for i, (role, _) in enumerate(messages) if role == "user"]
    first_user = user_idx[0] if user_idx else None
    target = user_idx[-1] if user_idx else None

    idx = _window_indices(messages, first_user)
    keep = idx.index(target) if target is not None else None
    # 정규화 전에 원문 기준으로 창 상한을 맞춘다(정규화는 글을 늘리지 않으므로 뒤에서 한 번 더 확인)
    raws = _fit_texts([messages[i][1] for i in idx], keep)

    turns: list[Turn] = []
    target_text = target_hidden = ""
    for i, raw in zip(idx, raws, strict=True):
        role = messages[i][0]
        text, hidden = _view(raw)
        is_target = i == target
        # 버튼 문구는 판단 대상이 아닌 첫 학습자 발화만 표시한다(검색 질의에서 뺀다).
        # 1턴 버튼 문구는 결정적 규칙 없이 주 호출이 판단한다(PRD §3)
        is_button = i == first_user and not is_target and is_button_phrase(text)
        if is_target:
            target_text, target_hidden = text, hidden
        turns.append(Turn(i, role, text, is_target, is_button))

    # 숨긴 문자 표기가 붙어 늘었을 수 있으니 정규화본으로 한 번 더 맞춘다
    texts = _fit_texts([t.text for t in turns], keep)
    turns = [
        Turn(t.index, t.role, s, t.is_target, t.is_button)
        for t, s in zip(turns, texts, strict=True)
    ]
    return Conversation(
        window=tuple(turns),
        target=target_text,
        target_raw=strip_tags(messages[target][1]) if target is not None else "",
        target_hidden=target_hidden,
        n_messages=len(messages),
        n_user=len(user_idx),
    )
