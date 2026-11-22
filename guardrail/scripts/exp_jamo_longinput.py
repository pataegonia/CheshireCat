"""실험 exp/guard-jamo-longinput 확인(LLM 0회). 저장소 루트에서
`python guardrail/scripts/exp_jamo_longinput.py`.

공개 세트 행(기존 `guardrail.eval.load.guard_rows`·`tutor_rows`, 홀드아웃 묶음 제외)에서 집계
숫자만 낸다. 문항 본문·행 id는 출력하지 않는다.

1. `input.jamo_profanity_not_low`를 켜고 끌 때 저신호 직접 판정이 바뀌는 행 수
   - 가드: `Guard._prepare`와 같은 입력(`clip` → `strip_tags`, 앞 대화 유무, 숨긴 문자)
   - 튜터: `direct_clarify`(대화 정리 `tidy` 뒤)
2. `input.max_chars` 2000 → 8000에서 전처리(`clip`) 결과가 바뀌는 행 수와 그 행들의 길이 분포
   (판정 창 `build_window`의 메시지와 판정 대상 가운데 하나라도 바뀌면 그 행을 센다)
"""

import math
import sys
from collections.abc import Sequence

from tutor.judge.conversation import tidy
from tutor.judge.signals import direct_clarify

from guardrail.eval.load import guard_rows, tutor_rows
from guardrail.judges.input_judge import build_window
from guardrail.preprocess import clip, decode_tags, strip_tags
from guardrail.signals import has_jamo_profanity, is_low_signal

WINDOW_LAST = 6  # 가드 기본 설정 input.window_last
BEFORE, AFTER = 2000, 8000


def _guard_low(messages: Sequence, *, jamo: bool, max_chars: int = BEFORE) -> bool | None:
    target = next((m.content for m in reversed(messages) if m.role == "user"), None)
    if target is None:
        return None
    clipped = clip(target, max_chars)
    pre = strip_tags(clipped)
    low = is_low_signal(pre, len(messages) > 1, decode_tags(clipped)[1])
    return False if low and jamo and has_jamo_profanity(pre) else low


def _tutor_low(messages: Sequence, *, jamo: bool) -> bool:
    conv = tidy([{"role": m.role, "content": m.content} for m in messages])
    return direct_clarify(conv, jamo_profanity_not_low=jamo)


def _clip_changes(messages: Sequence) -> int | None:
    """2000과 8000에서 전처리가 다른 메시지가 있으면 그 행의 가장 긴 메시지 길이, 없으면 None."""
    window = list(build_window(list(messages), WINDOW_LAST))
    target = next((m for m in reversed(messages) if m.role == "user"), None)
    pool = window + ([target] if target is not None else [])
    changed = [m for m in pool if clip(m.content, BEFORE) != clip(m.content, AFTER)]
    return max(len(m.content) for m in changed) if changed else None


def _pct(values: Sequence[int], q: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]


def main() -> int:
    out = sys.stdout
    for name, rows in (("guard", guard_rows()), ("tutor", tutor_rows())):
        g_flip = sum(
            _guard_low(r.messages, jamo=False) != _guard_low(r.messages, jamo=True) for r in rows
        )
        t_flip = sum(
            _tutor_low(r.messages, jamo=False) != _tutor_low(r.messages, jamo=True) for r in rows
        )
        lengths = [n for n in (_clip_changes(r.messages) for r in rows) if n is not None]
        out.write(f"[{name}] 행 {len(rows)}개(홀드아웃 묶음 제외)\n")
        out.write(f"  자모 토글 켬/끔에 따라 저신호가 바뀌는 행: 가드 {g_flip} · 튜터 {t_flip}\n")
        if lengths:
            out.write(
                f"  max_chars {BEFORE} → {AFTER}에서 전처리가 바뀌는 행: {len(lengths)}"
                f" (가장 긴 메시지 길이 최소 {min(lengths)} · 중앙 {_pct(lengths, 0.5)}"
                f" · 최대 {max(lengths)}자)\n"
            )
        else:
            out.write(f"  max_chars {BEFORE} → {AFTER}에서 전처리가 바뀌는 행: 0\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
