"""출력 근거 판정 nano (GR-07 ③, ADR-0014 R14 수정, C2-judges S2.7).

메시지 배치: system = `prompts/output_judge.md`(또는 베이스라인 프롬프트) 본문, user =
`<question>` → `<answer>` → `<kb id="섹션 ID">`(1위 섹션 전체 + 다른 섹션 조각).
태그에 넣는 데이터는 `html.escape`를 마지막에 건다(입력 판정과 같은 방식).
구간은 내지 않는다. nano가 낸 인용이 실제 부분 문자열인지는 `quote_ok`로 호출하는 쪽(S2.8)이 본다.
"""

import html
import re
from collections.abc import Sequence
from difflib import SequenceMatcher

from guardrail.config import GuardConfig, load_prompt
from guardrail.judges.schemas import OutputBaselineOut, OutputOut
from guardrail.judges.schemas_exp import OutputSentOut, SentCheck
from guardrail.kb import Hit, KbIndex, Ranking, normalize
from guardrail.llm import LLM, Role
from guardrail.preprocess import clip, decode_tags, reveal_hidden, strip_tags
from guardrail.rules import sentences

# 프로세스당 한 번 읽는다. 키 = (프롬프트 폴더, 파일 이름)
_SYSTEM_PROMPTS: dict[tuple[str, str], str] = {}
_MARK = re.compile(r"<U\+[0-9A-F]{4,6}>", re.I)  # reveal_hidden이 숨긴 문자를 드러낸 표기
MAX_SENTENCES = 20  # output.judge_mode: sentences에서 nano에 주는 문장 수 상한
REPAIR_MAX_QUOTE = 200  # 프롬프트 상한(80자)의 여유분. 넘으면 고치지 않는다
REPAIR_MIN_RATIO = 0.85  # repair_quote: 인용 글자 중 답변과 겹친 비율의 하한
REPAIR_MAX_STRETCH = 1.3  # repair_quote: 고친 구간 길이 / 인용 길이의 상한


def _system_prompt(cfg: GuardConfig, name: str) -> str:
    key = (cfg.paths.prompts, cfg.prompts.get(name, ""))
    if key not in _SYSTEM_PROMPTS:
        _SYSTEM_PROMPTS[key] = load_prompt(cfg, name)[1]
    return _SYSTEM_PROMPTS[key]


def output_context(ranking: Ranking, kb: KbIndex, cfg: GuardConfig) -> list[Hit]:
    """nano에 줄 문맥: 1위 섹션 전체 + 상위 `kb.top_k` 조각 중 다른 섹션의 조각(R14-D4).

    점수가 0인 조각은 쓰지 않는다(질의에 2-gram이 없으면 빈 목록).
    """
    top = [h for h in ranking.hits[: cfg.kb.top_k] if h.score > 0]
    if not top:
        return []
    first = top[0]
    whole = kb.section_text(first.section_id)[: cfg.output.context_max_chars]
    others = [h for h in top[1:] if h.section_id != first.section_id]
    return [Hit(first.section_id, whole, first.score), *others]


def _marks(s: str) -> set[str]:
    return {m.upper() for m in _MARK.findall(s)}


def quote_ok(quote: str, text: str, min_chars: int) -> bool:
    """인용이 text의 부분 문자열인가. 둘 다 채점 정규화(NFKC·소문자·공백·문장부호 제거) 뒤에 본다.

    nano는 `html.escape`된 글을 보고 옮기므로 인용의 엔티티(`&gt;` 등)를 먼저 되돌린다. text는
    이스케이프 전 원문(자른 답변, nano에 준 문맥)을 넘긴다. 답변은 `reveal_hidden`을 거치기 전
    원문이다. nano는 숨긴 문자가 `<U+200B>` 같은 표기로 바뀐 글을 보므로 양쪽을 `reveal_hidden`으로
    같은 모양으로 만든 뒤 `<U+XXXX>` 표기를 지우고 대조한다. 그래서 숨긴 문자에 걸친 인용은 표기를
    넣어 옮기든 빼고 옮기든 맞다. 태그 문자는 드러낸 ASCII를 넣은 인용도, 뺀 인용도 맞다.
    text에 없는 숨긴 문자 표기를 지어낸 인용, 정규화 뒤 빈 인용, `min_chars`자 미만 인용은 False다.
    """
    q = reveal_hidden(html.unescape(quote))
    shown = reveal_hidden(text)
    if not _marks(q) <= _marks(shown):
        return False
    q = normalize(_MARK.sub("", q))
    views = (shown, reveal_hidden(strip_tags(text)))  # 태그를 드러낸 글, 뺀 글
    return bool(q) and len(q) >= min_chars and any(q in normalize(_MARK.sub("", v)) for v in views)


def repair_quote(quote: str, answer: str, min_chars: int) -> str | None:
    """검증에 실패한 인용을 답변에서 가장 비슷한 구간(answer의 부분 문자열)으로 고친다. 없으면 None.

    일치 블록이 덮은 [첫 블록 시작, 마지막 블록 끝) 구간을 잡고, 인용 글자의 85% 이상이 겹치고
    구간이 인용의 1.3배 이하이며 정규화한 구간이 min_chars자 이상일 때만 돌려준다.
    """
    q = html.unescape(quote)
    if not q or len(q) > REPAIR_MAX_QUOTE:  # 긴 인용은 대조 비용이 답변 길이 × 인용 길이로 는다
        return None
    # 마지막 블록은 크기 0 표지라 뺀다(남기면 구간이 답변 끝까지 늘어난다)
    blocks = [
        b for b in SequenceMatcher(None, answer, q, autojunk=False).get_matching_blocks() if b.size
    ]
    if not blocks:
        return None
    start, end = blocks[0].a, blocks[-1].a + blocks[-1].size
    if sum(b.size for b in blocks) / len(q) < REPAIR_MIN_RATIO:
        return None
    if end - start > REPAIR_MAX_STRETCH * len(q):
        return None
    span = answer[start:end]
    return span if len(normalize(span)) >= min_chars else None


def sentence_texts(answer_revealed: str) -> list[str]:
    """nano에 보여 줄 문장들(앞 `MAX_SENTENCES`개). 문장 끝 줄바꿈·공백은 뗀다."""
    return [answer_revealed[s:e].strip() for s, e in sentences(answer_revealed)[:MAX_SENTENCES]]


def flagged_sentences(checks: Sequence[SentCheck], count: int) -> list[int]:
    """conflicts·not_in_material인 문장 번호(1..count, 중복 없이 오름차순)."""
    bad = {c.n for c in checks if c.verdict in ("conflicts", "not_in_material")}
    return sorted(n for n in bad if 1 <= n <= count)


def sentence_span(answer: str, revealed: str, flagged: Sequence[int]) -> str | None:
    """첫 flagged 문장을 원문 답변에서 글자 그대로. 두 글의 문장 수가 다르면 None."""
    if not flagged:
        return None
    cuts, shown = sentences(answer), sentences(revealed)
    if len(cuts) != len(shown) or flagged[0] > len(cuts):
        return None
    s, e = cuts[flagged[0] - 1]
    return answer[s:e].strip() or None


def build_messages(
    target: str,
    answer_revealed: str,
    hits: Sequence[Hit],
    cfg: GuardConfig,
    baseline: bool = False,
    by_sentence: bool = False,
) -> list[dict[str, str]]:
    """[system(고정 프롬프트), user(요청별 블록)]. by_sentence면 `<answer>` 뒤에 `<sentences>`.

    target은 마지막 학습자 발화 원문(여기서 자르고 정규화한다). answer_revealed는 호출하는 쪽이
    `input.max_answer_chars`로 자르고 `reveal_hidden`으로 숨긴 문자를 드러낸 답변이다.
    """
    question = html.escape(decode_tags(clip(target, cfg.input.max_chars))[0], quote=False)
    parts = [
        f"<question>{question}</question>",
        f"<answer>{html.escape(answer_revealed, quote=False)}</answer>",
    ]
    if by_sentence:
        texts = sentence_texts(answer_revealed)
        lines = [f"[{i}] {html.escape(t, quote=False)}" for i, t in enumerate(texts, 1)]
        parts.append("<sentences>\n" + "\n".join(lines) + "\n</sentences>")
    parts += [
        f'<kb id="{html.escape(h.section_id)}">{html.escape(h.text, quote=False)}</kb>'
        for h in hits
    ]
    name = (
        "output_judge_baseline"
        if baseline
        else "output_judge_sent"
        if by_sentence
        else "output_judge"
    )
    return [
        {"role": "system", "content": _system_prompt(cfg, name)},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


async def ask_grounded(
    target: str,
    answer_revealed: str,
    hits: Sequence[Hit],
    llm: LLM,
    cfg: GuardConfig,
    *,
    deadline: float,
    baseline: bool = False,
    retries: int | None = None,
    role: Role = "output_judge",
) -> OutputOut | OutputSentOut | OutputBaselineOut | None:
    """후보 답변의 근거 판정. 실패(재시도 후)·스키마 불일치·마감이면 None(기본값은 S2.8).

    retries가 None이면 `llm.retries`. role은 호출별 값(`llm.per_role`)과 호출 기록의 이름이다
    (프롬프트·스키마는 같다).
    """
    by_sentence = not baseline and cfg.output.judge_mode == "sentences"
    schema = OutputBaselineOut if baseline else OutputSentOut if by_sentence else OutputOut
    messages = build_messages(target, answer_revealed, hits, cfg, baseline, by_sentence)
    result = await llm.complete(role, messages, schema, deadline=deadline, retries=retries)
    return result.parsed if isinstance(result.parsed, schema) else None
