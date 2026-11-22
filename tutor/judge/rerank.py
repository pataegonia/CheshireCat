"""nano 섹션 재순위(실험, `toggles.rerank`, exp/tutor-rerank). 끄면 이 모듈은 아무것도 하지 않는다.

검색 후보(`llm.rerank.candidates`개, union이면 질의 임베딩 상위를 합쳐 최대 `candidates_max`개)와
대화를 rerank 프롬프트(`tutor/prompts/rerank.md`)에 보내 질문이 묻는 대상·속성을 실제로 담은
섹션 하나(`best`, 없으면 none)를 받는다. 흐름(`tutor.judge.flow`)은 BM25 검색 직후 이 호출을
띄우고(T와 동시에) 주 호출 직전에 결과를 받아 `apply`로 근거 묶음·규칙 A 1위를 고친다.

- nano 1회, 재시도 0, 튜터 LLM 게이트를 거친다(`TutorLLM.complete`). 마감은 min(띄운 때 +
  timeout_s, 요청 마감 − margin_s)이고 union의 질의 임베딩(마감 embedding_timeout_s)도 그 안이다.
- 실패·시간 초과·형식 오류·none·후보 밖 id면 지금 순서 그대로다.
- 기록은 코드(`Code`)와 ms뿐이다. 요청·섹션 본문, 고른 id는 남기지 않는다(TU-15). 요청 내용은
  지역 변수로만 쓴다(TU-18).
"""

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, create_model

from tutor.judge.conversation import Conversation
from tutor.judge.main_call import _turn_block, defuse

ROLE = "rerank"  # TutorLLM.calls에 세는 역할 이름
PROMPT_ID = "rerank"
PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "rerank.md"
NONE = "none"
WAIT_GRACE_S = 0.05
# called: 띄웠지만 결과를 쓰기 전에 요청이 끝났다(고정 문장 경로에서 취소). skipped: 부르지 않았다.
# failed: 오류·형식 오류·잘림·후보 밖 id. timeout: 마감 안에 안 옴. none: 고를 후보 없음.
# same: 고른 섹션이 이미 그 자리(바꿀 것 없음). changed: 순서·1위를 바꿨다
Code = Literal["called", "skipped", "failed", "timeout", "none", "same", "changed"]
_FRONT = re.compile(r"\A---\n(?P<head>.*?)\n---\n", re.S)
# 이 모듈이 더하는 구분 태그. 발화·자료 안의 같은 태그는 무력화한다(main_call.defuse와 같은 방식)
_TAG_RX = re.compile(r"<(?=\s*/?\s*(?:후\s*보|판\s*단\s*대\s*상))")


class RerankOut(BaseModel):
    """rerank 출력(json_schema strict, 필드 순서 = 생성 순서: 이유 먼저, 판정 마지막)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    reason: str
    best: str


@dataclass(frozen=True)
class RerankPrompt:
    version: str
    system: str

    @classmethod
    def load(cls, path: Path = PROMPT_PATH) -> "RerankPrompt":
        """머리말(---/id/version/---)을 확인하고 본문을 system 문구로 쓴다(main_call과 같은
        형식)."""
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        m = _FRONT.match(text)
        if not m:
            raise ValueError("rerank 프롬프트에 버전 머리말(---/id/version/---)이 없다")
        head = yaml.safe_load(m.group("head"))
        if not isinstance(head, dict) or head.get("id") != PROMPT_ID or not head.get("version"):
            raise ValueError("rerank 프롬프트 머리말 id·version이 맞지 않다")
        return cls(version=str(head["version"]), system=text[m.end() :].strip())


def _defuse(text: str) -> str:
    return _TAG_RX.sub("‹", defuse(text))


def candidate_block(section: Any, chars: int) -> str:
    """후보 하나: id·제목·본문 앞부분(chars 글자까지, 표 CSV는 넣지 않는다)."""
    body = section.text.strip()
    snippet = body[:chars] + ("…" if len(body) > chars else "")
    return f'<후보 id="{section.id}">\n제목: {_defuse(section.title)}\n{_defuse(snippet)}\n</후보>'


def build_messages(
    system: str, conv: Conversation, candidates: Sequence[Any], chars: int
) -> list[dict[str, str]]:
    """대화 창(학습자 글은 태그 안 데이터) · 판단 대상 · 후보 목록 세 블록."""
    target = next((t.text for t in conv.window if t.is_target), "")
    listing = "\n".join(candidate_block(s, chars) for s in candidates)
    user = (
        f"{_turn_block(conv)}\n\n<판단 대상>{_defuse(target)}</판단 대상>\n\n"
        f"<후보 목록>\n{listing}\n</후보 목록>"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def union_ids(bm25: Sequence[str], vector: Sequence[str], limit: int) -> list[str]:
    """BM25 순서 → 벡터에만 있는 것 순으로 합치고 중복을 뺀다(최대 limit개)."""
    return list(dict.fromkeys([*bm25, *vector]))[:limit]


def answer_schema(ids: Sequence[str]) -> type[RerankOut]:
    """이 요청의 출력 스키마: `best`의 json_schema에 후보 id + none을 enum으로 건다(strict
    json_schema면 모델이 후보 밖 글자를 낼 수 없다). 이름은 그대로 RerankOut이고 RerankOut의
    하위 클래스다. 받는 쪽 검증은 문자열 그대로라(enum을 검증에 쓰지 않는다) json_object 모드에서
    비슷하게 틀린 답도 `normalize_best`까지 온다. 요청마다 만들고 남기지 않는다(TU-18)."""
    enum = [*dict.fromkeys(ids), NONE]
    best = (str, Field(json_schema_extra={"enum": enum}))
    return create_model("RerankOut", __base__=RerankOut, best=best)  # type: ignore[call-overload]


_NONE_WORDS = frozenset({"none", "null", "nil", "n/a", "na", "-", "없음", "없다", "해당 없음"})
_WRAP = " \t\r\n\"'`“”‘’[](){}<>「」『』.,:;"
_SPACE = re.compile(r"\s+")
# 후보 밖 id의 모양(측정 전용, 정규화 전 원래 답 기준). exact가 아니면 recall --fail-detail이 센다
ID_SHAPES = ("exact", "case_space", "wrapped", "title", "none_word", "empty", "other")


def normalize_best(
    raw: str, ids: Sequence[str], titles: dict[str, str] | None = None
) -> tuple[str | None, str]:
    """모델의 `best` → (후보 id | "none" | None(쓸 수 없음), 모양).

    앞뒤 공백·따옴표·괄호를 벗기고, 대소문자·공백만 다른 id, 더 긴 글 안에 후보 id가 하나만 든
    경우(예: "후보 id=…", id 뒤 제목), 후보 제목을 그대로 낸 경우를 그 후보로 읽는다. 빈 값과
    none 뜻의 낱말은 none이다. 섹션 id와 조각 id는 같은 형식이라(KbIndex 조각 = 섹션 id) 조각
    번호가 붙은 답("id#2" 같은 꼴)은 감싼 글로 읽혀 그 섹션이 된다."""
    if raw in ids:
        return raw, "exact"
    s = raw.strip(_WRAP)
    if not s:
        return NONE, "empty"
    if s.lower() in _NONE_WORDS:
        return NONE, "none_word"
    key = _SPACE.sub("", s).casefold()
    by_key = {_SPACE.sub("", i).casefold(): i for i in ids}
    if key in by_key:
        return by_key[key], "case_space"
    inside = [i for k, i in by_key.items() if k in key]
    if len(inside) == 1:
        return inside[0], "wrapped"
    if titles:
        want = _SPACE.sub(" ", s).casefold()
        named = [i for i in ids if _SPACE.sub(" ", titles.get(i, "")).strip().casefold() == want]
        if len(named) == 1:
            return named[0], "title"
    return None, "other"


def judge(result: Any, ids: Sequence[str]) -> tuple[Code, str | None]:
    """호출 결과 → (코드, 고른 id). 고른 id는 후보 안의 id일 때만."""
    code, best, _fail = judge_detail(result, ids)
    return code, best


# failed의 갈래(측정 전용, recall --rerank --fail-detail). 값은 고정 낱말과 오류 코드뿐이다
# format: 스키마에 안 맞음(JSON 아님·필드 틀림). length: finish_reason=length(잘림).
# out_of_candidates: 후보 밖 id. exception: 호출이 예외를 냈다. api:<코드>: 그 밖의 API 오류
FAIL_KINDS = ("format", "length", "out_of_candidates", "exception")


def judge_detail(
    result: Any, ids: Sequence[str], titles: dict[str, str] | None = None
) -> tuple[Code, str | None, str | None]:
    """`judge`와 같고, failed면 그 갈래(`FAIL_KINDS` 또는 `api:<오류 코드>`)도 낸다.
    `best`는 `normalize_best`로 읽는다(titles를 주면 제목으로 낸 답도 읽는다)."""
    if result.error == "timeout":
        return "timeout", None, None
    if result.finish_reason == "length":
        return "failed", None, "length"
    if result.error == "bad_json":
        return "failed", None, "format"
    if result.error is not None:
        return "failed", None, f"api:{result.error}"
    if not isinstance(result.parsed, RerankOut):
        return "failed", None, "format"
    best, _shape = normalize_best(result.parsed.best, ids, titles)
    if best == NONE:
        return "none", None, None
    if best is None:
        return "failed", None, "out_of_candidates"
    return "called", best, None  # 쓸 수 있는 답(same·changed는 apply가 정한다)


def id_shape(result: Any, ids: Sequence[str], titles: dict[str, str] | None = None) -> str | None:
    """측정 전용: 스키마를 통과한 답의 `best` 모양(`ID_SHAPES`). 답이 없으면 None."""
    if result is None or result.error is not None or not isinstance(result.parsed, RerankOut):
        return None
    return normalize_best(result.parsed.best, ids, titles)[1]


@dataclass
class Outcome:
    """rerank 한 번의 결과(요청 안에서만). 본문은 없다."""

    code: Code
    best: str | None = None  # 후보 안의 id(쓸 수 있을 때만)
    candidates: tuple[str, ...] = ()
    union: bool = False  # 후보에 질의 임베딩 상위를 합쳤나
    embed_calls: int = 0  # 이 rerank가 부른 질의 임베딩 수(0·1)
    fail: str | None = None  # failed의 갈래(judge_detail). 측정 전용, 로그에 내지 않는다
    shape: str | None = None  # 답의 id 모양(id_shape, 정규화 전). 측정 전용, 로그에 내지 않는다
    ms: float = 0.0


@dataclass
class RerankJob:
    """띄운 rerank. `until`(루프 시각)이 지나면 결과를 기다리지 않는다."""

    task: "asyncio.Task[Outcome]"
    until: float
    started: float

    def cancel(self) -> None:
        if not self.task.done():
            self.task.cancel()


async def vector_ids(emb: Any, text: str, *, timeout_s: float) -> list[str] | None:
    """질의 임베딩 순위(섹션 ID). 기동 때 만든 임베딩 검색(`tutor_emb`)의 섹션 벡터를 그대로
    쓰고, 질의 하나만 임베딩한다(게이트 대기 포함 timeout_s). 실패·늦으면 None."""
    if timeout_s <= 0:
        return None
    try:
        async with asyncio.timeout(timeout_s):
            async with emb.gate():
                vectors = await emb.embed([text])
        return emb.ranking(vectors[0])
    except asyncio.CancelledError:
        raise
    except Exception:  # 시간 초과·API 오류·모양이 다른 벡터: bm25 후보로 돌아간다
        return None


async def _run(
    llm: Any,
    prompt: RerankPrompt,
    conv: Conversation,
    sections: Sequence[Any],
    index: Any,
    rr: Any,
    *,
    until: float,
    emb: Any,
    text: str,
) -> Outcome:
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    pool = list(sections[: rr.candidates])
    ids = [s.id for s in pool]
    out = Outcome("called", candidates=tuple(ids))
    if emb is not None:  # union(한국어 발화)
        out.embed_calls = 1
        vec = await vector_ids(emb, text, timeout_s=min(rr.embedding_timeout_s, until - t0))
        if vec:
            merged = union_ids(ids, vec[: rr.candidates], rr.candidates_max)
            pool = [index.section(sid) for sid in merged]
            ids, out.union, out.candidates = merged, True, tuple(merged)
    left = until - loop.time()
    if not ids or left <= 0:
        out.code = "timeout" if ids else "skipped"
        out.ms = (loop.time() - t0) * 1000
        return out
    messages = build_messages(prompt.system, conv, pool, rr.snippet_chars)
    try:
        result = await llm.complete(
            ROLE,
            messages,
            answer_schema(ids),  # best에 후보 id enum(strict json_schema)
            deadline=until,
            timeout_s=left,
            retries=0,
            effort=rr.reasoning_effort,
            max_completion_tokens=rr.max_completion_tokens,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # 메시지는 남기지 않는다
        result = None
    if result is None:
        out.code, out.best, out.fail = "failed", None, "exception"
    else:
        titles = {s.id: s.title for s in pool}
        out.code, out.best, out.fail = judge_detail(result, ids, titles)
        out.shape = id_shape(result, ids, titles)
    out.ms = (loop.time() - t0) * 1000
    return out


def start_rerank(
    llm: Any,
    prompt: RerankPrompt,
    conv: Conversation,
    sections: Sequence[Any],
    index: Any,
    rr: Any,
    *,
    deadline: float,
    emb: Any = None,
    text: str = "",
) -> RerankJob | None:
    """rerank를 띄운다. 마감은 min(지금 + timeout_s, 요청 마감 − margin_s). 시간이 없으면 None.
    `emb`를 주면(union, 한국어 발화) 먼저 질의 임베딩 상위를 후보에 합친다(`text` = 검색 질의)."""
    now = asyncio.get_running_loop().time()
    until = min(now + rr.timeout_s, deadline - rr.margin_s)
    if until <= now or not sections:
        return None
    task = asyncio.ensure_future(
        _run(llm, prompt, conv, sections, index, rr, until=until, emb=emb, text=text)
    )
    return RerankJob(task, until + WAIT_GRACE_S, now)


async def wait_rerank(job: RerankJob) -> Outcome:
    """결과를 받는다. 마감이 지나도 안 끝나면 취소하고 timeout."""
    loop = asyncio.get_running_loop()
    done, _ = await asyncio.wait({job.task}, timeout=max(0.0, job.until - loop.time()))
    if job.task not in done or job.task.cancelled():
        job.cancel()
        return Outcome("timeout", ms=(loop.time() - job.started) * 1000)
    return job.task.result()


def reorder(bundle: Sequence[Any], best: Any) -> list[Any]:
    """고른 섹션을 맨 앞으로. 묶음 밖이면 맨 앞에 넣고 마지막을 빼 길이를 그대로 둔다."""
    if any(s.id == best.id for s in bundle):
        return [best, *(s for s in bundle if s.id != best.id)]
    return [best, *bundle[: max(len(bundle) - 1, 0)]]


@dataclass(frozen=True)
class Applied:
    code: Code
    bundle: tuple[Any, ...]  # 주 호출 근거 묶음(바뀌지 않았으면 그대로)
    top: list[Any] | None  # top1 모드에서 바꾼 규칙 A 목록(1위 = 고른 섹션). 안 바꿨으면 None


def apply(
    mode: str, outcome: Outcome, bundle: Sequence[Any], rule_a: Sequence[Any] | None, index: Any
) -> Applied:
    """rerank 결과를 근거 묶음·규칙 A 1위에 반영한다.

    context: 근거 묶음에서 고른 섹션을 맨 앞으로만(규칙 A 1위·cite_top1은 그대로).
    top1: 근거 묶음 순서를 바꾸고 규칙 A 1위도 고른 섹션으로. 고른 섹션이 규칙 A 1위(규칙 A가
    없으면 근거 묶음 1위)와 같으면 아무것도 바꾸지 않는다."""
    bundle = tuple(bundle)
    if outcome.best is None:
        return Applied(outcome.code, bundle, None)
    best = index.section(outcome.best)
    if mode == "top1":
        first = (rule_a or bundle or [None])[0]
        if first is not None and first.id == best.id:
            return Applied("same", bundle, None)
        top = None if rule_a is None else [best, *(s for s in rule_a if s.id != best.id)]
        return Applied("changed", tuple(reorder(bundle, best)), top)
    if bundle and bundle[0].id == best.id:
        return Applied("same", bundle, None)
    return Applied("changed", tuple(reorder(bundle, best)), None)
