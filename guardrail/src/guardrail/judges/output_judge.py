"""출력 근거 판정 nano (GR-07 ③, ADR-0014 R14 수정, C2-judges S2.7).

메시지 배치: system = `prompts/output_judge.md`(또는 베이스라인 프롬프트) 본문, user =
`<question>` → `<answer>` → `<kb id="섹션 ID">`(1위 섹션 전체 + 다른 섹션 조각).
태그에 넣는 데이터는 `html.escape`를 마지막에 건다(입력 판정과 같은 방식).
구간은 내지 않는다. nano가 낸 인용이 실제 부분 문자열인지는 `quote_ok`로 호출하는 쪽(S2.8)이 본다.
"""

import html
import re
from collections.abc import Sequence

from guardrail.config import GuardConfig, load_prompt
from guardrail.judges.schemas import OutputBaselineOut, OutputOut
from guardrail.kb import Hit, KbIndex, Ranking, normalize
from guardrail.llm import LLM
from guardrail.preprocess import clip, decode_tags, reveal_hidden, strip_tags

# 프로세스당 한 번 읽는다. 키 = (프롬프트 폴더, 파일 이름)
_SYSTEM_PROMPTS: dict[tuple[str, str], str] = {}
_MARK = re.compile(r"<U\+[0-9A-F]{4,6}>", re.I)  # reveal_hidden이 숨긴 문자를 드러낸 표기


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


def build_messages(
    target: str,
    answer_revealed: str,
    hits: Sequence[Hit],
    cfg: GuardConfig,
    baseline: bool = False,
) -> list[dict[str, str]]:
    """[system(고정 프롬프트), user(요청별 블록)].

    target은 마지막 학습자 발화 원문(여기서 자르고 정규화한다). answer_revealed는 호출하는 쪽이
    `input.max_answer_chars`로 자르고 `reveal_hidden`으로 숨긴 문자를 드러낸 답변이다.
    """
    question = html.escape(decode_tags(clip(target, cfg.input.max_chars))[0], quote=False)
    parts = [
        f"<question>{question}</question>",
        f"<answer>{html.escape(answer_revealed, quote=False)}</answer>",
    ]
    parts += [
        f'<kb id="{html.escape(h.section_id)}">{html.escape(h.text, quote=False)}</kb>'
        for h in hits
    ]
    name = "output_judge_baseline" if baseline else "output_judge"
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
) -> OutputOut | OutputBaselineOut | None:
    """후보 답변의 근거 판정. 실패(재시도 후)·스키마 불일치·마감이면 None(기본값은 S2.8).

    retries가 None이면 `llm.retries`.
    """
    schema = OutputBaselineOut if baseline else OutputOut
    messages = build_messages(target, answer_revealed, hits, cfg, baseline)
    result = await llm.complete(
        "output_judge", messages, schema, deadline=deadline, retries=retries
    )
    return result.parsed if isinstance(result.parsed, schema) else None
