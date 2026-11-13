"""입력 판정 nano (GR-04, ADR-0012·0013, C2-judges S2.6).

메시지 배치: system = `prompts/input_judge.md` 본문(요청마다 같은 접두라 프롬프트 캐시가 맞는다),
user = `<examples>` → `<conversation>` → `<target>` → (`oom_kb_snippet`) `<kb>`.
태그에 넣는 데이터는 보이지 않는 문자만 먼저 지우고 `input.max_chars`로 자르고(넘으면 공백·반복을
줄인 뒤 앞 + 뒤, `preprocess.clip`) → 정규화(숨긴 태그 문자는 드러냄) → `html.escape` 순서로
만든다. 자모 조합 같은 무거운 정규화는 자른 뒤에 한다(1MB 메시지를 전처리하면 CPU 약 1.3초).
숨긴 태그 문자가 있으면 드러낸 표식(" [숨긴 문자: …]")만큼 상한을 넘을 수 있다.
알려진 한계: few-shot 예시(`LabeledRow.text` = `normalize`)는 태그 문자를 지운 본이라 표식이
없다(공개 400행에 태그 문자가 든 발화는 0건).
"""

import html
from collections.abc import Sequence

from guardrail.config import GuardConfig, load_prompt
from guardrail.data import LabeledRow
from guardrail.judges.schemas import ACT_TO_CATEGORY, InputActOut, InputOut
from guardrail.llm import LLM
from guardrail.preprocess import clip, decode_tags
from guardrail.schema import Category, Message

_ROLES = frozenset({"user", "assistant"})
# 프로세스당 한 번 읽는다(요청마다 디스크 읽기·YAML 파싱을 하지 않고, 평가 중 파일이 바뀌어도
# 같은 system 메시지). 키 = (프롬프트 폴더, 파일 이름)
_SYSTEM_PROMPTS: dict[tuple[str, str], str] = {}


def _system_prompt(cfg: GuardConfig) -> str:
    key = (cfg.paths.prompts, cfg.prompts.get("input_judge", ""))
    if key not in _SYSTEM_PROMPTS:
        _SYSTEM_PROMPTS[key] = load_prompt(cfg, "input_judge")[1]
    return _SYSTEM_PROMPTS[key]


def build_window(messages: Sequence[Message], last: int) -> list[Message]:
    """첫 학습자 발화 + 마지막 `last`개. 첫 학습자 발화가 이미 그 안에 있으면 한 번만."""
    msgs = list(messages)
    start = max(len(msgs) - last, 0)
    first = next((i for i, m in enumerate(msgs) if m.role == "user"), None)
    if first is None or first >= start:
        return msgs[start:]
    return [msgs[first], *msgs[start:]]


def _data(text: str, cfg: GuardConfig) -> str:
    """보이지 않는 문자 제거·상한(앞 + 뒤) → 정규화(태그 문자는 [숨긴 문자: …]로) → escape."""
    return html.escape(decode_tags(clip(text, cfg.input.max_chars))[0], quote=False)


def _examples(examples: Sequence[LabeledRow], cfg: GuardConfig) -> str:
    # LabeledRow.text는 이미 정규화본이다. 순서는 kNN이 정한 그대로(가장 비슷한 것 마지막).
    # input.knn.order가 nearest_first면 뒤집는다(S3.2 선별용 변형)
    if cfg.input.knn.order == "nearest_first":
        examples = list(reversed(examples))
    lines = [
        f'<example category="{ex.category}">'
        f"{html.escape(ex.text[: cfg.input.max_chars], quote=False)}</example>"
        for ex in examples
    ]
    return "<examples>\n" + "\n".join(lines) + "\n</examples>"


def _conversation(window: Sequence[Message], cfg: GuardConfig) -> str:
    # role은 요청 값이라 속성에 그대로 넣지 않는다
    lines = [
        f'<message role="{m.role if m.role in _ROLES else "other"}">'
        f"{_data(m.content, cfg)}</message>"
        for m in build_window(window, cfg.input.window_last)
    ]
    return "<conversation>\n" + "\n".join(lines) + "\n</conversation>"


def build_messages(
    window: Sequence[Message],
    target: str,
    examples: Sequence[LabeledRow],
    cfg: GuardConfig,
    kb_snippet: str | None = None,
) -> list[dict[str, str]]:
    """[system(고정 프롬프트), user(요청별 블록)]. window는 대화 전체를 넘겨도 된다.

    `<conversation>`에는 대상 발화도 들어 있다(창의 마지막 메시지). `<target>`은 판정 대상을
    따로 가리킨다.
    """
    parts = []
    if cfg.toggles.knn_fewshot and examples:
        parts.append(_examples(examples, cfg))
    parts.append(_conversation(window, cfg))
    parts.append(f"<target>{_data(target, cfg)}</target>")
    if cfg.toggles.oom_kb_snippet and kb_snippet:
        snippet = html.escape(kb_snippet[: cfg.input.oom_snippet_chars], quote=False)
        parts.append(f"<kb>{snippet}</kb>")
    return [
        {"role": "system", "content": _system_prompt(cfg)},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


async def judge_input(
    window: list[Message],
    target: str,
    examples: list[LabeledRow],
    llm: LLM,
    cfg: GuardConfig,
    *,
    deadline: float,
    kb_snippet: str | None = None,
) -> Category | None:
    """마지막 학습자 발화의 범주. 실패(재시도 후)·스키마 불일치·마감이면 None(기본값은 S2.8).

    window는 대화 전체를 넘겨도 된다(`build_window`로 자른다). 재시도는 `LLM.complete`가
    일시적 오류에만 1회 한다.
    """
    messages = build_messages(window, target, examples, cfg, kb_snippet)
    if cfg.input.label_mode == "act":  # 요청 유형을 받아 범주로 대응시킨다(S3.2 선별용 변형)
        result = await llm.complete("input_judge", messages, InputActOut, deadline=deadline)
        parsed = result.parsed
        return ACT_TO_CATEGORY[parsed.act] if isinstance(parsed, InputActOut) else None
    result = await llm.complete("input_judge", messages, InputOut, deadline=deadline)
    return result.parsed.category if isinstance(result.parsed, InputOut) else None
