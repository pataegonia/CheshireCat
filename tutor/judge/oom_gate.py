"""자료 밖 확인 E(`toggles.oom_gate`, #213 B2의 E). nano 1회, 주 호출과 동시에.

요청이 시작될 때 검색 상위 k 근거 묶음(BM25, 주 호출과 같은 질의·k. `toggles.embedding`을 켜도
E는 BM25만 쓴다, B2와 같다. 영어 발화는 `toggles.english_embedding`일 때만 임베딩 순위)과
대화를 E 프롬프트(`tutor/prompts/oom_check.md`, #162 v0과 같은 글)에 보내 "마지막 질문이 묻는
것이 근거 묶음에 있는가"(found)를 받는다. 흐름이 끝나 최종 action이 answer이고 E가
found=false면 자료 밖 고정 문장(abstain)으로 바꾼다.

- 판단 대상 원문(정규화)에 한글이 한 글자도 없고 라틴 글자가 있으면(영어 발화) E를 부르지
  않는다(#213 결론 문서 2장의 좁힌 영어 규칙. `toggles.english_embedding`을 켜고 기동 때
  임베딩을 만들었으면 부른다). B2 실험 서버는 "라틴 > 한글"로 쟀고, 문서는 공개
  교차 검증에서 두 규칙이 막는 E '없음' 행이 같다고 적었다.
- E는 E 검색이 끝난 때부터 `llm.timeout_s.oom_gate`초 안에 끝나야 쓴다(검색은 ms 단위). 넘거나
  실패·잘림이면 흐름 결과 그대로다(E는 abstain으로만 바꾸므로 실패가 답을 막지 않는다).
- 측정(#213, yang-co1 11/17): B2(주 호출 ∥ E v0, 마감 9초, 고정 문장 abstain, 영어 규칙 + 인용 1위
  규칙 A)는 공개 교차 검증 판정 포함 M 대비 +2.6(자료 밖 질문 63.5 → 84.4), 대회 실제 제출 튜터
  +3.7. 규칙 A는 이 토글에 넣지 않았다(점검 세트 18 → 18).
- 재시도 없음. 요청 내용은 지역 변수로만 쓴다(TU-18). 사유는 횟수만 센다.
"""

import asyncio
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from tutor.judge.conversation import Conversation
from tutor.judge.main_call import _section_block, _turn_block

ROLE = "oom_gate"  # TutorLLM.calls에 세는 역할 이름
PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "oom_check.md"
EMPTY_BUNDLE = "(관련 자료를 찾지 못했습니다)"  # main_call.build_user_message와 같은 문구
WAIT_GRACE_S = 0.05
_HANGUL = re.compile(r"[가-힣ㄱ-ㆎ]")
_LATIN = re.compile(r"[A-Za-z]")
# no_time · error · truncated · invalid · timeout · found · absent · english → 횟수
GateCounts = Counter[str]


class OomOut(BaseModel):
    """E 출력(json_schema strict, 필드 순서 = 생성 순서, harness `OomCheckOut`과 같다)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    asked: str
    quote: str
    found: bool


def load_system(path: Path = PROMPT_PATH) -> str:
    """system 문구. 첫 줄 출처 주석(<!-- … -->)은 빼고 나머지를 글자 그대로(앞뒤 빈 줄만 뺀다)."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    first, _, rest = text.partition("\n")
    if not (first.startswith("<!--") and first.endswith("-->")):
        raise ValueError(f"{path.name} 첫 줄에 출처 주석이 없다")
    return rest.strip("\n")


def english_only(text: str) -> bool:
    """한글(완성 음절·호환 자모)이 한 글자도 없고 라틴 글자가 있다. `text`는 정규화한 원문이어야
    한다(풀어 쓴 자모는 정규화 뒤에 완성 음절이 된다)."""
    return _HANGUL.search(text) is None and _LATIN.search(text) is not None


def build_messages(
    system: str, sections: Sequence[Any], conv: Conversation
) -> list[dict[str, str]]:
    """주 호출과 같은 근거 묶음·대화 창 두 블록(신호·판단 예시는 넣지 않는다)."""
    bundle = "\n".join(_section_block(s) for s in sections) or EMPTY_BUNDLE
    user = f"<근거 묶음>\n{bundle}\n</근거 묶음>\n\n{_turn_block(conv)}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _count(counts: GateCounts | None, reason: str) -> None:
    if counts is not None:
        counts[reason] += 1


@dataclass(frozen=True)
class GateJob:
    """시작한 E 호출. `until`(루프 시각)이 지나면 결과를 기다리지 않는다."""

    task: "asyncio.Task[OomOut | None]"  # E 출력(실패·잘림이면 None)
    until: float

    def cancel(self) -> None:
        if not self.task.done():
            self.task.cancel()


async def _call(
    llm: Any, messages: list[dict[str, str]], deadline: float, limit: float, counts
) -> OomOut | None:
    """E 출력(asked·quote·found). 실패·잘림이면 None."""
    try:
        result = await llm.complete(
            ROLE, messages, OomOut, deadline=deadline, timeout_s=limit, retries=0
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # 메시지는 남기지 않는다
        _count(counts, "error")
        return None
    if result.finish_reason == "length":
        _count(counts, "truncated")
        return None
    if result.error is not None or not isinstance(result.parsed, OomOut):
        _count(counts, "error" if result.error is not None else "invalid")
        return None
    return result.parsed


def start_gate(
    llm: Any,
    messages: list[dict[str, str]],
    *,
    timeout_s: float,
    deadline: float,
    counts: GateCounts | None = None,
) -> GateJob | None:
    """E 호출을 시작한다. 마감은 지금부터 `timeout_s`와 요청 마감 중 이른 쪽. 시간이 없으면 None."""
    now = asyncio.get_running_loop().time()
    limit = min(timeout_s, deadline - now)
    if limit <= 0:
        _count(counts, "no_time")
        return None
    task = asyncio.ensure_future(_call(llm, messages, min(deadline, now + limit), limit, counts))
    return GateJob(task, now + limit + WAIT_GRACE_S)


async def material_absent(job: GateJob, counts: GateCounts | None = None) -> bool:
    """E가 마감 안에 found=false를 냈으면 True. 시간 초과면 취소하고 False(흐름 결과 유지)."""
    timeout = max(0.0, job.until - asyncio.get_running_loop().time())
    done, _ = await asyncio.wait({job.task}, timeout=timeout)
    if job.task not in done:
        job.cancel()
        _count(counts, "timeout")
        return False
    if job.task.cancelled():
        return False
    out = job.task.result()
    if out is None:
        return False
    found = out.found
    _count(counts, "found" if found else "absent")
    return found is False
