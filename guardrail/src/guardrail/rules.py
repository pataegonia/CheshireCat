"""S2.4 숫자·리터럴 규칙 ② (GR-06 추출, GR-07 ②, ADR-0014, master-plan §4.1).

이식 원본: scripts/r13/q5_ungrounded.py의 NUM_RX, scripts/r13/q6_rule_fp.py의 V2·merged_pm4_spans
(import하지 않음). V2 = NUM_RX에서 두 곳만 고친 것:
- 추출 전에 섹션 ID 인용(CITE_RX)을 지운다. 토큰의 start·end는 원문 위치로 되돌린다.
- ⓕ의 글자 클래스 [한두세네다섯]("다 번"에도 걸림)을 (?:한|두|세|네|다섯)으로 바꾼다.
비교 = 토큰과 섹션 양쪽에 scoring.normalize(NFKC는 여기서만) 뒤 부분 문자열 포함.
"""

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from guardrail.config import GuardConfig
from guardrail.kb import Hit, KbIndex

# competition/grader/scoring.py의 PUNCT·normalize 복사(런타임에 grader를 import하지 않음)
PUNCT = frozenset(".,!?;:'\"“”‘’`()[]{}<>«»·…~-–—/\\")


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", str(s)).lower()
    return "".join(ch for ch in s if not ch.isspace() and ch not in PUNCT)


CITE_RX = re.compile(r"\(?\b(?:PY|PR|FIN)-\d{2}-\d{2}\b\)?")
_UNITS = r"만\s?원|천\s?원|원|%|세|개월|점|번|개|씩|배|단계|권|명|회|줄"
_UNITS_EXTRA = r"|주|일|년|글자"
_REST = (
    r"|(?P<decimal>\d+(?:[.,]\d+)+)|(?P<multi_digit>\d{2,})|(?P<num_list>\[[\d, ]+\])"
    r"|(?P<latin>[A-Za-z_][A-Za-z_0-9.()]*(?:\s[A-Za-z_]+)*)"
    r"|(?P<numeral>(?:한|두|세|네|다섯)\s?(?:번|개|단계|가지|배))"
)
# 그룹 이름 = LiteralToken.kind(ⓐ unit_number … ⓕ numeral). 매칭은 NUM_RX V2와 같다(테스트)
_TOKEN_RX = re.compile(rf"(?P<unit_number>\d+(?:[.,]\d+)*\s?(?:{_UNITS}))" + _REST)
_TOKEN_RX_UNITS_EXTRA = re.compile(
    rf"(?P<unit_number>\d+(?:[.,]\d+)*\s?(?:{_UNITS}{_UNITS_EXTRA}))" + _REST
)
_CODE_RX = re.compile(r"```.*?```|`[^`]*`", re.S)
_MONEY_RX = re.compile(r"(?<![가-힣])(\d+(?:[.,]\d+)*)?\s?([억만천])\s?원")
_MULT = {"천": 1000, "만": 10_000, "억": 100_000_000}

EXTRA_TOKENS = frozenset({"value_norm", "units_extra", "bare_numeral", "backtick_latin"})
_UNMEASURED = frozenset({"bare_numeral"})  # 토큰 경계가 문서에 없다(PR "미정")


@dataclass(frozen=True)
class LiteralToken:
    kind: str  # unit_number | decimal | multi_digit | num_list | latin | numeral (ⓐ–ⓕ)
    value: str  # scoring.normalize(토큰). value_norm이면 금액을 숫자로 바꾼 뒤
    start: int  # 원문 위치
    end: int


def _check_extra(extra: Iterable[str]) -> frozenset[str]:
    ex = frozenset(extra)
    if ex - EXTRA_TOKENS:
        raise ValueError(f"모르는 rule_extra_tokens: {sorted(ex - EXTRA_TOKENS)}")
    if ex & _UNMEASURED:
        raise NotImplementedError(f"rule_extra_tokens 미구현: {sorted(ex & _UNMEASURED)}")
    return ex


def _money(m: re.Match[str]) -> str:
    num = Decimal(m.group(1).replace(",", "")) if m.group(1) else Decimal(1)
    return f"{(num * _MULT[m.group(2)]).normalize():f}원"


def _value(text: str, ex: frozenset[str]) -> str:
    # shortcut: "2만 5천 원"처럼 단위가 둘인 금액은 따로 바뀐다. ablation에서 쓰이면 고친다
    return normalize(_MONEY_RX.sub(_money, text) if "value_norm" in ex else text)


def _strip_cites(text: str) -> tuple[str, list[int]]:
    """인용을 지운 문자열과, 남은 글자마다의 원문 위치."""
    parts: list[str] = []
    pos: list[int] = []
    last = 0
    for m in CITE_RX.finditer(text):
        parts.append(text[last : m.start()])
        pos.extend(range(last, m.start()))
        last = m.end()
    parts.append(text[last:])
    pos.extend(range(last, len(text)))
    return "".join(parts), pos


def extract_literals(text_raw: str, extra: Iterable[str] = ()) -> list[LiteralToken]:
    """원문에서 V2 토큰을 순서대로 뽑는다. extra = output.rule_extra_tokens."""
    ex = _check_extra(extra)
    stripped, pos = _strip_cites(text_raw)
    rx = _TOKEN_RX_UNITS_EXTRA if "units_extra" in ex else _TOKEN_RX
    code = [m.span() for m in _CODE_RX.finditer(text_raw)] if "backtick_latin" in ex else []
    out = []
    for m in rx.finditer(stripped):
        start, end = pos[m.start()], pos[m.end() - 1] + 1
        kind = m.lastgroup or ""
        if code and kind == "latin" and not any(s <= start and end <= e for s, e in code):
            continue
        out.append(LiteralToken(kind, _value(m.group(), ex), start, end))
    return out


def unsupported_literals(
    answer_raw: str, ref_text: str, extra: Iterable[str] = ()
) -> list[LiteralToken]:
    """답변 토큰 중 value가 정규화한 참조 텍스트에 부분 문자열로 없는 것."""
    ex = _check_extra(extra)
    ref = _value(ref_text, ex)
    return [t for t in extract_literals(answer_raw, ex) if t.value not in ref]


def _eojeol(answer: str, i: int, j: int) -> tuple[int, int]:
    while i > 0 and not answer[i - 1].isspace():
        i -= 1
    while j < len(answer) and not answer[j].isspace():
        j += 1
    return i, j


def rule_spans(
    answer_raw: str, tokens: Sequence[LiteralToken], mode: str = "pm4", k: int = 4
) -> tuple[str, ...]:
    """걸린 토큰 → 원문 부분 문자열 구간.

    서로 다른 토큰 문자열마다 답변에서 처음 나온 위치 하나에 창(pm4: ±k자, eojeol: 어절)을 만들고,
    겹치거나 맞닿은 창은 합치고, 같은 구간은 한 번만, normalize 뒤 빈 구간은 버린다.
    """
    if mode not in ("pm4", "eojeol"):
        raise ValueError(f"모르는 rule_span: {mode}")
    wins = set()
    for text in dict.fromkeys(answer_raw[t.start : t.end] for t in tokens):
        if not text:
            continue
        i = answer_raw.find(text)  # 첫 위치(같은 토큰이 여러 번 나와도 창 하나)
        j = i + len(text)
        wins.add((max(0, i - k), j + k) if mode == "pm4" else _eojeol(answer_raw, i, j))
    merged: list[tuple[int, int]] = []
    for s, e in sorted(wins):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    spans = dict.fromkeys(answer_raw[s:e] for s, e in merged)
    return tuple(x for x in spans if normalize(x))


def literal_rule(
    answer_raw: str, hits: Sequence[Hit], margin: float, kb: KbIndex, cfg: GuardConfig
) -> tuple[str, ...] | None:
    """GR-07 ②. 안 걸리면 None, 걸리면 구간(rule_emit_spans: false면 빈 튜플).

    hits·margin은 guard.py가 kb.rank(query) 한 번으로 얻은 같은 Ranking의 값이다.
    """
    out = cfg.output
    if out.rule_require_same_kind:
        raise NotImplementedError("rule_require_same_kind 미구현(종류 표와 ⓐ–ⓕ 대응이 미정)")
    if not hits or margin < out.rule_min_margin:
        return None
    ref = kb.section_text(hits[0].section_id)
    toks = unsupported_literals(answer_raw, ref, out.rule_extra_tokens)
    if not toks:
        return None
    if not out.rule_emit_spans:
        return ()
    return rule_spans(answer_raw, toks, out.rule_span)
