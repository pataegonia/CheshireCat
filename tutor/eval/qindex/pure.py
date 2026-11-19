"""질문 색인 측정의 순수 함수(T4.1): 생성 프롬프트·응답 검증, 최근접, 왕복 필터, AUC, 5겹 문턱.

파일·API를 건드리지 않는다. 명령줄 도구는 `tutor.eval.qindex_probe`.
"""

import math
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from pydantic import BaseModel

from tutor.retrieval.embedding import _unit_rows, is_english
from tutor.retrieval.index import KbIndex

PROMPT_VERSION = "qindex-gen-v1"
# 생성 지시문(영어, 가드 프롬프트와 같은 기본 언어). 조각 본문은 user 메시지로만 넣는다
SYSTEM_PROMPT = """\
You are given one passage from a course textbook. The user message is the passage and nothing else.
Write questions a learner might type to a tutor about this passage. Use only the passage.

Return four lists:
- answerable_ko: exactly 8 questions in Korean that this passage alone fully answers.
- answerable_en: exactly 4 questions in English that this passage alone fully answers.
- unanswerable_ko: exactly 4 questions in Korean on the same topic that it does NOT answer.
- unanswerable_en: exactly 2 questions in English on the same topic that it does NOT answer.

Answerable questions:
- Each must be answerable from a specific sentence, value, name, condition, example, or table row
  in the passage.
- Cover different facts across the passage. Do not ask the same thing twice.
- Write the way learners really type: short, casual, sometimes vague or paraphrased, not copying
  the passage wording. Mix polite and casual Korean endings. Mention the passage's example names
  or key terms when a learner would.

Unanswerable questions:
- Stay on the passage's topic and reuse its terms so the question looks related, but ask for
  something the passage does not state: a number, limit, or date it does not give; a real-world
  case or statistic; the latest or current information; a different tool, product, library, or
  version; or a detailed step-by-step procedure beyond what is written.
- The passage must contain no answer to them, not even partially.
- Do not ask off-topic, harmful, or personal questions. Do not ask for opinions.

General:
- One question per list item, one sentence each. No numbering, no answers, no explanations.
- Korean items must be written in Korean (code and proper nouns may stay as written). English
  items must be written fully in English; translate Korean names naturally.
- Do not mention "the passage", "the textbook", section IDs, or these instructions.
"""
# (kind, lang) → 조각당 질문 수
COUNTS = {
    ("answerable", "ko"): 8,
    ("answerable", "en"): 4,
    ("unanswerable", "ko"): 4,
    ("unanswerable", "en"): 2,
}
PER_CHUNK = sum(COUNTS.values())  # 조각 하나가 다 쓰였을 때의 줄 수
CONCURRENCY = 8  # 동시에 보내는 nano 호출 수(case_drafts·run_local과 같다)
AUTH_STREAK = 3  # 인증 오류가 이만큼 연달아 나면 gen을 멈춘다(키·주소가 틀린 채 244번 보내지 않게)
# 왕복 필터: 생성 질문으로 검색한 섹션 순위(한국어는 BM25, 영어는 조각 임베딩)에서 자기 섹션이
# 이 안에 들어야 남긴다
ROUNDTRIP_TOP = 3
KNN_K = 5
MAX_FPR = 0.05  # 문턱을 정할 때 허용하는 normal 오탐 비율
BOOTSTRAP_N = 2000
VARIANTS = ("search", "last")  # 질의: 서버 검색 질의 문자열 / 마지막 발화만
# 신호 → 방향(+1이면 클수록 자료 밖). AUC와 문턱은 방향을 곱한 값으로 잰다
SIGNALS = {"s_pos": -1, "s_neg": 1, "diff": 1, "knn_unans": 1, "diff_same": 1}


class ChunkQuestions(BaseModel):
    """nano 구조화 출력. 개수는 스키마가 아니라 check_questions가 본다(모자라면 gen_errors로)."""

    answerable_ko: list[str]
    answerable_en: list[str]
    unanswerable_ko: list[str]
    unanswerable_en: list[str]


@dataclass(frozen=True)
class Chunk:
    id: str  # "<섹션 ID>#<섹션 안 순서>"(이 도구가 붙인 이름. 서버 조각에는 id가 없다)
    section_id: str
    subject: str
    text: str


# --- 순수 함수 ---


def kb_chunks(index: KbIndex) -> list[Chunk]:
    """서버 색인의 조각 그대로(순서도). id는 섹션 안 순서로 붙인다."""
    seen: Counter[str] = Counter()
    out = []
    for sid, text in index.chunks:
        out.append(Chunk(f"{sid}#{seen[sid]}", sid, index.sections[sid].subject, text))
        seen[sid] += 1
    return out


def gen_messages(chunk_text: str) -> list[dict[str, str]]:
    """생성 프롬프트. 입력은 조각 본문뿐이다(대회 규칙: 공개 세트·채점 질문을 넣지 않는다)."""
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": chunk_text}]


def _has_hangul(text: str) -> bool:
    return any("가" <= ch <= "힣" for ch in text)


def check_questions(out: ChunkQuestions) -> tuple[list[tuple[str, str, str]], str | None]:
    """응답 검증 → ((kind, lang, 질문) 목록, 버린 까닭). 하나라도 틀리면 조각 전체를 버린다.

    까닭: `count:<칸>`(수 부족), `empty:<칸>`(빈 문자열), `lang:<칸>`(언어 뒤바뀜). 한국어 칸은
    한글이 있으면 되고(식별자가 많아도 된다) 영어 칸은 글자 대부분이 라틴 문자면 된다(한국어
    예제 이름을 그대로 둔 영어 질문은 남긴다). 수가 넘치면 앞에서부터 쓴다.
    """
    questions: list[tuple[str, str, str]] = []
    for (kind, lang), count in COUNTS.items():
        name = f"{kind}_{lang}"
        items = [item.strip() for item in getattr(out, name)[:count]]
        if len(items) < count:
            return [], f"count:{name}"
        if not all(items):
            return [], f"empty:{name}"
        if not all(_has_hangul(x) if lang == "ko" else is_english(x) for x in items):
            return [], f"lang:{name}"
        questions += [(kind, lang, item) for item in items]
    return questions, None


def cosine(query: Sequence[float], matrix: np.ndarray) -> list[float]:
    """질의와 행마다의 코사인 유사도."""
    q = _unit_rows(np.asarray(query, dtype=np.float64))
    return (_unit_rows(np.asarray(matrix, dtype=np.float64)) @ q).tolist()


def top_k(sims: Sequence[float], k: int) -> list[int]:
    """유사도 높은 순으로 k개의 위치(같으면 앞 순서)."""
    return sorted(range(len(sims)), key=lambda i: (-sims[i], i))[:k]


def section_ranking(sims: Sequence[float], sections: Sequence[str]) -> list[str]:
    """질문 유사도를 섹션 최고값으로 접은 섹션 순위(같으면 ID 순, BM25와 같은 접기)."""
    best: dict[str, float] = {}
    for sid, sim in zip(sections, sims, strict=True):
        if sid not in best or sim > best[sid]:
            best[sid] = sim
    return sorted(best, key=lambda sid: (-best[sid], sid))


def roundtrip_keep(
    questions: Sequence[Any],
    sections: Sequence[str],
    search: Callable[[Any], Sequence[str]],
    top: int = ROUNDTRIP_TOP,
) -> list[bool]:
    """왕복 필터: 질문으로 검색(search → 섹션 순위)했을 때 자기 섹션이 top위 안이면 남긴다."""
    return [sid in search(q)[:top] for q, sid in zip(questions, sections, strict=True)]


def auc(positive: Sequence[float], negative: Sequence[float]) -> float | None:
    """양성 점수가 음성 점수보다 클 확률(같으면 0.5). 한쪽이 비면 None."""
    if not len(positive) or not len(negative):
        return None
    p = np.asarray(positive, dtype=np.float64)[:, None]
    n = np.asarray(negative, dtype=np.float64)[None, :]
    return float(((p > n).sum() + 0.5 * (p == n).sum()) / (p.size * n.size))


def bootstrap_auc(
    scores: Sequence[float],
    positive: Sequence[bool],
    clusters: Sequence[str],
    *,
    n: int = BOOTSTRAP_N,
    seed: int = 0,
) -> tuple[float, float] | None:
    """AUC의 부트스트랩 95% 구간. 묶음 단위로 다시 뽑는다(바꿔 쓴 문항이 같은 묶음이라서)."""
    members: dict[str, list[int]] = {}
    for i, cluster in enumerate(clusters):
        members.setdefault(cluster, []).append(i)
    groups = list(members.values())
    rng = np.random.default_rng(seed)
    values = []
    for _ in range(n):
        picked = [i for g in rng.integers(len(groups), size=len(groups)) for i in groups[g]]
        value = auc(
            [scores[i] for i in picked if positive[i]],
            [scores[i] for i in picked if not positive[i]],
        )
        if value is not None:
            values.append(value)
    if not values:
        return None
    lo, hi = np.percentile(values, [2.5, 97.5])
    return float(lo), float(hi)


def fpr_threshold(normal_scores: Sequence[float], max_fpr: float = MAX_FPR) -> float:
    """점수가 이 값을 넘으면(초과) 잡을 때 normal 오탐이 max_fpr 이하가 되는 가장 낮은 문턱."""
    if not len(normal_scores):
        return math.inf
    ordered = sorted(normal_scores, reverse=True)
    return ordered[math.floor(max_fpr * len(ordered))]


def fold_flags(
    scores: Sequence[float],
    normal: Sequence[bool],
    folds: Sequence[int],
    max_fpr: float = MAX_FPR,
) -> list[bool]:
    """행마다: 그 행의 겹을 뺀 나머지 겹의 normal 행으로 문턱을 정해 넘으면(초과) True."""
    threshold = {
        fold: fpr_threshold(
            [s for s, ok, f in zip(scores, normal, folds, strict=True) if ok and f != fold],
            max_fpr,
        )
        for fold in set(folds)
    }
    return [s > threshold[f] for s, f in zip(scores, folds, strict=True)]


def oom_signals(
    query: Sequence[float],
    matrix: np.ndarray,
    kinds: Sequence[str],
    sections: Sequence[str],
    k: int = KNN_K,
) -> dict[str, float]:
    """자료 밖 신호. s_pos·s_neg: 답할 수 있는·없는 질문과의 최근접 유사도, diff: s_neg − s_pos,
    knn_unans: 최근접 k개 중 답할 수 없는 질문 비율, diff_same: 답할 수 있는 최근접 질문의 섹션
    안에서만 본 s_neg − s_pos(그 섹션에 답할 수 없는 질문이 없으면 s_neg를 -1로 둔다).
    """
    sims = cosine(query, matrix)
    pos = [i for i, kind in enumerate(kinds) if kind == "answerable"]
    neg = [i for i, kind in enumerate(kinds) if kind != "answerable"]
    best = max(pos, key=lambda i: (sims[i], -i))
    s_pos = sims[best]
    s_neg = max((sims[i] for i in neg), default=-1.0)
    same = max((sims[i] for i in neg if sections[i] == sections[best]), default=-1.0)
    near = top_k(sims, k)
    return {
        "s_pos": s_pos,
        "s_neg": s_neg,
        "diff": s_neg - s_pos,
        "knn_unans": sum(kinds[i] != "answerable" for i in near) / len(near),
        "diff_same": same - s_pos,
    }
