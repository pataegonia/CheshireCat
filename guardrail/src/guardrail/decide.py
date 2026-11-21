"""판정 조립의 순수 함수 (PRD GR-07 표 ①–④·GR-08·GR-09, ADR-0014, C2-judges S2.8).

입출력만 있고 부작용이 없다. 호출·마감·기록은 guard.py가 한다.
"""

from collections import Counter
from collections.abc import Sequence
from typing import Literal

from guardrail.config import Toggles
from guardrail.schema import Category
from guardrail.signals import Signals

Tier = Literal[1, 2, 3, 4]


def fallback_category(knn_major: Category | None, lr_pred: Category | None) -> Category:
    """입력 nano가 실패했을 때의 범주(GR-09). 꺼진 쪽은 None으로 받는다.

    둘이 같으면 그 범주, 다르면 LR, 한쪽만 있으면 그쪽, 둘 다 없으면 normal.
    """
    return lr_pred or knn_major or "normal"


def ensemble_vote(votes: Sequence[Category | None], block: frozenset[Category]) -> Category | None:
    """input.ensemble: 투표자 A·B·C 순서의 범주(None = 무효)를 하나로. 모두 무효면 None.

    차단 여부는 유효 표의 다수결, 동수(유효 2표가 갈림)면 첫 유효 표(A, A가 무효면 B) 쪽.
    차단이면 차단 표 가운데 가장 많은 범주(동수면 투표자 순서). 허용이면 A가 허용이었을 때 A의
    범주, 아니면 허용 표 가운데 가장 많은 범주(동수면 B, C 순서). 유효 표가 A 하나면 A 그대로.
    """
    valid = [v for v in votes if v is not None]
    if not valid:
        return None
    blocks = [v for v in valid if v in block]
    allows = [v for v in valid if v not in block]
    is_block = len(blocks) > len(allows) or (len(blocks) == len(allows) and valid[0] in block)
    first = votes[0]
    if not is_block and first is not None and first not in block:
        return first
    side = blocks if is_block else allows
    counts = Counter(side)
    return max(side, key=counts.__getitem__)  # max는 동수면 앞의 것(투표자 순서)을 낸다


def request_category(first: Category, asks_kinds: Sequence[str]) -> Category | None:
    """S3.13 요청 재확인: normal·answer_seeking + offcourse_output면 off_topic. None = 그대로.

    S3.13 1단: finished_work_to_submit 경로는 21건 중 20건 오답이라 뺐다
    (results/2026-11-17-paired-compare.md). 그 종류는 범주를 바꾸지 않고 기록만 한다.
    """
    if first in ("normal", "answer_seeking") and "offcourse_output" in asks_kinds:
        return "off_topic"
    return None


def recheck_block(first: Category, asks_kinds: Sequence[str]) -> Category:
    """S3.13 차단 재확인: 1차 off_topic만, 차단 종류 없이 learn이 있으면 normal. 아니면 1차 그대로.

    1차 academic_dishonesty를 normal로 푸는 경로는 8건 중 7건 오답이라 뺐다.
    """
    if first != "off_topic":
        return first
    if {"finished_work_to_submit", "offcourse_output"} & set(asks_kinds):
        return first
    return "normal" if "learn" in asks_kinds else first


def trusted(mode: str, answer_quote_ok: bool, kb_quote_ok: bool) -> bool:
    """nano의 "근거 없음"이 `output.tier3_trust` 조건을 채웠는가(R14-D2)."""
    if mode == "all":
        return True
    if mode == "answer_quote":
        return answer_quote_ok
    if mode == "both_quotes":
        return answer_quote_ok and kb_quote_ok
    raise ValueError(f"output.tier3_trust에 없는 값: {mode!r}")


def output_verdict(
    category: Category,
    block: frozenset[Category],
    rule_spans: tuple[str, ...] | None,
    nano_ungrounded: bool | None,
    trusted: bool,
    toggles: Toggles,
    failure_default: Literal["grounded", "ungrounded"],
    quote_span: str | None = None,
) -> tuple[bool, tuple[str, ...], Tier]:
    """(grounded, unsupported, 단). GR-07 표를 위에서부터 본다.

    rule_spans: 규칙 ②가 안 걸렸으면 None, 걸렸으면 구간(구간을 안 내는 설정이면 빈 튜플).
    nano_ungrounded: 출력 nano가 근거 없음이라고 했는가. 실패했거나 부르지 않았으면 None.
    quote_span: 자른 원문에 글자 그대로 있는 answer_quote. `nano_span`이 켜졌을 때만 ③의 구간.
    """
    if category in block:  # ① 토글로 끄지 않는다
        return True, (), 1
    if toggles.literal_rule and rule_spans is not None:  # ②
        return False, tuple(rule_spans), 2
    if toggles.nano_tier3 and nano_ungrounded is not None:
        if not nano_ungrounded:  # ④ nano가 근거 있음이라고 했다
            return True, (), 4
        if trusted:  # ③
            return False, (quote_span,) if toggles.nano_span and quote_span else (), 3
    # ④ nano 판정을 쓰지 않는 경우: 실패, ③ 꺼짐, 근거 없음이지만 인용 조건 미달
    return failure_default == "grounded", (), 4


def safe_of(category: Category, block: frozenset[Category], sig: Signals, toggles: Toggles) -> bool:
    """GR-08: block 범주에 응한 답변과 숨긴 태그가 든 답변만 safe=false. LLM을 쓰지 않는다."""
    if not toggles.safe_rule:
        return True
    if sig.hidden_tags:
        return False
    return category not in block or sig.refusal
