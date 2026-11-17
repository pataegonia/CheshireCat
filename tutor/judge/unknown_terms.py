"""KB에 없는 이름 붙은 말 찾기(`toggles.oom_gate: named_term`). LLM 호출 0회.

마지막 학습자 발화에서 "앞말 + 머리말(규칙·지수·분류법·등급·기준·수수료·규정·한도·상태 …)"을 찾아,
합친 말(공백·기호 없음)이 KB 전체 글에 한 번도 없으면 그 말을 돌려준다. 자료 밖 확인 E를 이런
발화에서만 부르는 데 쓴다(E가 정상 질문을 잘못 막는 몫과 E 호출 수를 줄인다). 이 함수만으로
abstain을 내지는 않는다(공개 세트에서 정상·개인정보 질문 5행도 걸린다).

- 머리말 목록은 공개 세트의 자료 밖 질문이 묻는 꼴(이름 붙은 규칙·지수·분류법·등급)을 보고 팀이
  골랐다. 공개 세트에 맞춘 위험이 있어 점검 세트(공개 밖)의 오탐도 같이 본다(PRD TU-06).
- KB 글은 기동 때 한 번 모은다(배포 자료 가공, SPEC §7.1). 요청 내용은 지역 변수로만 쓴다(TU-18).
"""

import re
from collections.abc import Mapping
from typing import Any

HEADS = "규칙|지수|분류법|등급|기준|수수료|규정|한도|상태|원칙|제도|정책|공식|점수제|체계"
_TERM = re.compile(rf"(?:(?<=\s)|^)([가-힣A-Za-z]{{2,8}})\s?({HEADS})")
_PARTICLE = re.compile(r"(의|을|를|은|는|이|가|에서|에|로|으로|과|와)$")
# 앞말이 관형·지시어거나 용언·부사 꼴이면 이름이 아니다("누군지 규칙", "아닌 규칙", "보기 준")
_STOP = frozenset(
    {
        "이런",
        "그런",
        "어떤",
        "무슨",
        "어느",
        "그럼",
        "우리",
        "필요한",
        "새로운",
        "다른",
        "같은",
        "정한",
    }
)
_VERBISH = re.compile(r"(지|고|게|면|서|며|는|던|한|할|인|닌|운|른|하|기|도|만|요|죠|까)$")


def squash(text: str) -> str:
    """비교용: 한글·영문·숫자만 남기고 소문자로."""
    return re.sub(r"[^가-힣A-Za-z0-9]", "", text).lower()


def kb_blob(sections: Mapping[str, Any]) -> str:
    """KB 섹션 본문·표 전체를 비교용으로 이어 붙인 글(기동 때 한 번)."""
    parts: list[str] = []
    for section in sections.values():
        parts.append(section.text)
        parts.extend(section.tables)
    return squash("\n".join(parts))


def missing_terms(text: str, blob: str) -> list[str]:
    """발화에서 찾은 이름 붙은 말 중 KB 글에 없는 것(찾은 순서, 중복 없음)."""
    out: list[str] = []
    for m in _TERM.finditer(text):
        word = _PARTICLE.sub("", m.group(1))
        if len(word) < 2 or word in _STOP or _VERBISH.search(word):
            continue
        term = word + m.group(2)
        if squash(term) not in blob and term not in out:
            out.append(term)
    return out
