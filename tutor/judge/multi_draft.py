"""주 호출 여러 초안 + 결정적 고르기(실험, `llm.multi_draft`). 끄면 이 모듈은 아무것도 하지 않는다.

같은 근거 묶음·같은 메시지로 주 호출을 K번 동시에 보내고(초안 A = 지금의 주 호출, B = 같은
low 호출 한 번 더, C = medium), 모델 호출 없이 하나를 고른다. 고른 초안은 지금과 같은 뒤 단계
(대행 보정·T 덮기·E·결정적 수정·후처리)를 그대로 지난다. 그래서 투표가 정하는 것은 주 호출이
내는 (범주, action, 문장)뿐이다. 직렬 T가 주 호출 없이 끝내는 요청(refuse·위기 고정 문장)에는
초안이 하나도 없다.

고르기(`select`)
1. 유효한 초안(스키마를 통과한 것)의 action 다수결. 동률이면 medium 초안의 action(유효하고 동률
   안에 있으면), 아니면 초안 A의 action, 아니면 번호가 가장 작은 초안의 action.
2. 그 action을 낸 초안마다 claim 문장 가운데 인용 문구가 그 섹션 원문에 실제로 있는(통합
   결정적 수정 `_verified_sids`와 같은 검사) 문장의 비율 → 확인된 인용 수 → medium → A → 번호
   순으로 고른다.
   claim이 없는 초안의 비율은 0이다.

기다리기(`wait_drafts`): 모두 끝나면 바로. 아니면 `cut`(= min(시작 + wait_s, 마감 − margin_s))에
유효한 초안이 있으면 남은 것을 취소한다. 없으면 하나가 유효해지거나 모두 끝나거나 요청 마감이 올
때까지 기다린다(마지막 길을 먼저 끊지 않는다). 빠져나갈 때 남은 작업은 모두 취소한다(TU-18).

기록(`Shadow`): 요청 하나의 컨텍스트에만 있는 메타데이터(수·지연·오류 코드·번호). 라우터가 접근 로그
끝 칸으로 낸다. action·범주 값은 로그 칸에 넣지 않는다(TU-15): A 혼자였다면 같은 action이었는지만
0/1로 낸다. action 값(`a_action`·`chosen_action`)은 로컬 측정(run_local)이 행별 결과에만 쓴다.
"""

import asyncio
from collections import Counter
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from integration.actions import FinalizeConfig, _verified_sids
from integration.contracts import Section, Sentence
from tutor.judge.main_call import MainCallOut
from tutor.judge.specialists import StageRec

LETTERS = "abcdefgh"  # 초안 번호 → 로그 키 글자(로그 키에는 숫자를 쓰지 않는다)
MEDIUM = "medium"
MIN_QUOTE_CHARS = FinalizeConfig.model_fields["min_quote_chars"].default  # 결정적 수정과 같은 값
EFFORT_KEY = {"low": "low", "medium": "med"}


@dataclass
class DraftStat:
    """초안 하나의 메타데이터(본문 없음)."""

    key: str  # 로그 키: effort 줄임 + "_" + 초안 글자(예: low_a, med_c). effort를 못 보내면 na_c
    ms: float
    attempts: int
    code: str  # "-"(성공) | 오류 코드 | length·content_filter(잘림) | cut(기다리기에서 취소)
    gate_ms: float


@dataclass
class Shadow:
    """요청 하나의 여러 초안 기록. 라우터·측정 도구가 ContextVar로 넣는다(요청 사이 공유 없음)."""

    started: int = 0
    valid: int = 0
    chosen: int | None = None
    split: bool = False  # 유효한 초안끼리 action이 갈렸나
    a_action: str | None = None  # 측정 전용(로그에 내지 않는다)
    chosen_action: str | None = None  # 측정 전용(로그에 내지 않는다)
    actions: list[str | None] = field(default_factory=list)  # 초안별 action(측정 전용, 무효 None)
    # 실험 단계 기록(tutor.judge.specialists). 키 rq·sj·mm, 토글이 off인 단계는 없다
    stages: dict[str, StageRec] = field(default_factory=dict)
    drafts: list[DraftStat] = field(default_factory=list)
    sent: Counter[str] = field(default_factory=Counter)  # 이 요청의 튜터 LLM 시도 수(역할별)
    # 측정 전용(로그에 내지 않는다, 섹션 ID). 초안별로 그 초안이 골라졌다면 같은 결정적 인용 경로가
    # 냈을 인용(정렬, 무효 초안 None). 응답이 고른 초안의 assemble 경로까지 온 요청에만 있다
    md_cites: list[list[str] | None] | None = None
    # 그중 모델이 인용한 섹션(결정적 수정이 고른 확인된 인용, 1위 채우기·cite_top1 추가 제외)
    md_model_cites: list[list[str] | None] | None = None
    top1: str | None = None  # cite_top1이 더하는 섹션(모든 초안에 같다). 끔이면 None
    # 최종 응답이 고른 초안에서 그대로 나갔나(cite_rule이 적용되는 요청). E·rq가 바꾸면 False
    cite_path: bool = False

    def log_field(self) -> str | None:
        """접근 로그 끝 칸. 초안을 하나도 보내지 않았으면 None(칸 없음).

        `calls=튜터 nano 시도 수;md_n=시작;md_ok=유효;md_split=0|1[;md_pick=번호;md_a_same=0|1]`
        뒤에 초안마다 `;effort_글자=ms/보낸 횟수/코드/게이트 대기 ms`. 가드 /guard 칸과 같은 모양.
        실험 단계 토글이 off가 아니면 끝에 `;rq=돌았나/신뢰도/판정 코드/ms;rq_ch=바꿨나`
        (sj·mm도 같은 모양)를 더한다. 초안도 단계 기록도 없으면 None.
        """
        if not self.started and not self.stages:
            return None
        parts = [f"calls={sum(self.sent.values())}"]
        if self.started:
            parts += [
                f"md_n={self.started}",
                f"md_ok={self.valid}",
                f"md_split={int(self.split)}",
            ]
        if self.chosen is not None:
            parts.append(f"md_pick={self.chosen}")
            if self.a_action is not None:
                parts.append(f"md_a_same={int(self.a_action == self.chosen_action)}")
        parts += [f"{d.key}={d.ms:.0f}/{d.attempts}/{d.code}/{d.gate_ms:.0f}" for d in self.drafts]
        for key, rec in self.stages.items():
            parts += rec.log_parts(key)
        return ";".join(parts)


SHADOW: ContextVar[Shadow | None] = ContextVar("tutor_multi_draft", default=None)


def contract_sections(sections: Sequence[Any]) -> list[Section]:
    """검색 결과 → 통합 `Section`(인용 문구 검사 입력)."""
    return [
        Section(
            id=s.id,
            subject=s.subject,
            title=s.title,
            text=s.text,
            tables=list(s.tables),
            names=list(s.names),
        )
        for s in sections
    ]


def verified_sids(out: MainCallOut, sections: Sequence[Section]) -> set[str]:
    """claim 문장의 인용 문구가 그 섹션 원문에 실제로 있는 섹션(결정적 수정과 같은 검사)."""
    return {
        sid
        for s in out.sentences
        if s.kind == "claim"
        for sid in _verified_sids(
            Sentence.model_validate(s.model_dump()), sections, MIN_QUOTE_CHARS
        )
    }


def evidence_score(out: MainCallOut, sections: Sequence[Section]) -> tuple[float, int]:
    """(근거가 확인된 claim 문장 비율, 확인된 인용 수). claim이 없으면 (0, 0)."""
    claims = [s for s in out.sentences if s.kind == "claim"]
    if not claims:
        return 0.0, 0
    verified = [
        _verified_sids(Sentence.model_validate(s.model_dump()), sections, MIN_QUOTE_CHARS)
        for s in claims
    ]
    return sum(bool(v) for v in verified) / len(claims), sum(len(v) for v in verified)


def select(
    outs: Sequence[MainCallOut | None], efforts: Sequence[str], sections: Sequence[Section]
) -> int | None:
    """고를 초안 번호. 유효한 초안이 없으면 None."""
    valid = [i for i, o in enumerate(outs) if o is not None]
    if not valid:
        return None
    medium = next((i for i in valid if efforts[i] == MEDIUM), None)
    votes = Counter(outs[i].action for i in valid)  # type: ignore[union-attr]
    top = max(votes.values())
    tied = [i for i in valid if votes[outs[i].action] == top]  # type: ignore[union-attr]
    lead = medium if medium in tied else (0 if 0 in tied else tied[0])
    action = outs[lead].action  # type: ignore[union-attr]

    def rank(i: int) -> tuple[float, int, bool, bool, int]:
        share, count = evidence_score(outs[i], sections)  # type: ignore[arg-type]
        return share, count, i == medium, i == 0, -i

    return max((i for i in valid if outs[i].action == action), key=rank)  # type: ignore[union-attr]


def abstain_override(outs: Sequence[MainCallOut | None], chosen: int | None) -> int | None:
    """`abstain_if_any`: 고른 초안의 action이 answer인데 유효한 초안 가운데 abstain이 있으면 그 첫
    초안의 번호, 아니면 None. 고른 action이 answer 밖(scaffold·clarify…)이면 바꾸지 않는다."""
    if chosen is None or outs[chosen].action != "answer":  # type: ignore[union-attr]
        return None
    return next((i for i, o in enumerate(outs) if o is not None and o.action == "abstain"), None)


CITE_RULES = (
    "pick",
    "vote",
    "pick_drop_lone",
    "pick_add_vote",
    "single_model",
    "single_support",
    "single_support_model",
    "single_agree",
)


def compose_cites(
    rule: str,
    pick: Sequence[str],
    md_cites: Sequence[Sequence[str] | None],
    actions: Sequence[str | None],
    chosen: int,
    md_model: Sequence[Sequence[str] | None] | None = None,
) -> list[str]:
    """`llm.multi_draft.cite_rule`: 고른 초안의 인용(`pick`, 응답에 나간 순서)을 같은 action
    초안들의 인용(`md_cites`, 초안 번호 순)으로 고친다. 호출 0회. pick이 비면 그대로 둔다
    (인용 없는 action). 같은 action 초안 = 유효하고 action이 고른 초안과 같은 초안(고른 초안 포함).

    vote: 같은 action 초안 2개 이상이 인용한 섹션(비면 pick).
    pick_drop_lone: pick에서 같은 action의 다른 초안이 하나도 인용하지 않은 섹션을 뺀다(비면 pick).
    pick_add_vote: pick ∪ vote. 순서는 pick 먼저, 그다음 초안 번호 순.
    single_*는 초안별 모델 인용(`md_model`, cite_top1 추가·1위 채우기 제외)이 있어야 한다:
    single_model: pick의 모델 인용만(없으면 pick).
    single_support: pick이 2개 이상이면 같은 action 초안 가운데 그 섹션을 인용 목록에 가진 수가
    가장 많은 섹션 하나(동률이면 pick). single_support_model: 같은 계산을 모델 인용으로.
    single_agree: 같은 action 초안의 모델 인용이 모두 같으면 그것만(아니면 pick)."""
    if rule == "pick" or not pick:
        return list(pick)
    if rule.startswith("single_"):
        if md_model is None:
            raise ValueError(f"{rule}에는 초안별 모델 인용(md_model)이 있어야 한다")
        same = [
            i for i, c in enumerate(md_cites) if c is not None and actions[i] == actions[chosen]
        ]
        mine = set(md_model[chosen] or [])
        pm = [s for s in pick if s in mine]
        if rule == "single_model":
            return pm or list(pick)
        if rule == "single_agree":
            agree = all(set(md_model[i] or []) == mine for i in same)
            return pm if pm and agree else list(pick)
        if rule in ("single_support", "single_support_model"):
            lists = md_cites if rule == "single_support" else md_model
            support = {s: sum(s in (lists[i] or []) for i in same) for s in pick}
            best = [s for s in pick if support[s] == max(support.values())]
            return best if len(pick) >= 2 and len(best) == 1 else list(pick)
        raise ValueError(f"모르는 cite_rule: {rule}")
    others = [
        c
        for i, c in enumerate(md_cites)
        if i != chosen and c is not None and actions[i] == actions[chosen]
    ]
    counts = Counter(s for c in [pick, *others] for s in set(c))
    voted = [s for s in dict.fromkeys([*pick, *(s for c in others for s in c)]) if counts[s] >= 2]
    if rule == "vote":
        return voted or list(pick)
    if rule == "pick_drop_lone":
        return [s for s in pick if counts[s] >= 2] or list(pick)
    if rule == "pick_add_vote":
        return list(dict.fromkeys([*pick, *voted]))
    raise ValueError(f"모르는 cite_rule: {rule}")


async def wait_drafts(
    tasks: Sequence["asyncio.Future[Any]"],
    *,
    cut: float,
    deadline: float,
    valid: Callable[[Any], bool],
) -> None:
    """위 기다리기 규칙. 돌아올 때 끝나지 않은 작업은 취소돼 있다. 취소는 삼키지 않는다."""
    loop = asyncio.get_running_loop()
    pending = set(tasks)
    try:
        left = cut - loop.time()
        if left > 0:
            _, pending = await asyncio.wait(pending, timeout=left)
        while pending and not any(t.done() and valid(t.result()) for t in tasks):
            left = deadline - loop.time()
            if left <= 0:
                return
            _, pending = await asyncio.wait(
                pending, timeout=left, return_when=asyncio.FIRST_COMPLETED
            )
    finally:
        for task in pending:
            task.cancel()
