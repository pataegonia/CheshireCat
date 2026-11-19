"""질문 색인 측정의 계산과 화면 출력(T4.1). 입력은 이미 읽은 행·벡터뿐이다(파일·API 없음).

보고서와 행별 결과에는 행 id·섹션 id·수만 담는다(질문 본문 없음, TU-15).
"""

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TextIO

import numpy as np

from tutor.eval.qindex.pure import (
    MAX_FPR,
    SIGNALS,
    VARIANTS,
    auc,
    bootstrap_auc,
    fold_flags,
    oom_signals,
    roundtrip_keep,
    section_ranking,
)
from tutor.retrieval.embedding import _unit_rows, rrf


@dataclass(frozen=True)
class QuestionIndex:
    lines: tuple[dict[str, Any], ...]  # questions.jsonl 줄(emb 행 순서)
    matrix: np.ndarray  # 단위 벡터(질문 수 × 차원)
    model: str  # 임베딩 모델 이름(질의·조각 임베딩과 같아야 한다)


@dataclass(frozen=True)
class DevRow:
    id: str
    fold: int
    cluster: str
    category: str
    action: str
    gold: tuple[str, ...]  # 정답 섹션(없으면 빈 튜플)
    english: bool  # 마지막 발화가 영어 위주
    search: str  # 서버 검색 질의 문자열(retrieve가 만드는 것과 같다)
    last: str  # 마지막 발화만
    rule_a: str  # 규칙 A 질의(튜터 발화까지, toggles.top1_dialog_query)
    history: tuple[str, ...]  # retrieve에 넘기는 앞 학습자 발화(서버 방식 재현용)


def _hit_table(rows: Sequence[DevRow], tops: Sequence[tuple[bool, bool]]) -> dict[str, Any]:
    """(1위 적중, 5위 적중) 목록을 전체·언어·과목별로 센다."""
    groups: dict[str, list[tuple[bool, bool]]] = {}
    for row, hit in zip(rows, tops, strict=True):
        for name in ("all", "en" if row.english else "ko", row.gold[0].split("-")[0]):
            groups.setdefault(name, []).append(hit)
    return {
        name: {"n": len(hits), "hit1": sum(h[0] for h in hits), "hit5": sum(h[1] for h in hits)}
        for name, hits in sorted(groups.items())
    }


def _hit(gold: Sequence[str], ranked: Sequence[str]) -> tuple[bool, bool]:
    return any(g in ranked[:1] for g in gold), any(g in ranked[:5] for g in gold)


def evaluate(
    rows: Sequence[DevRow],
    qindex: QuestionIndex,
    vectors: Mapping[str, np.ndarray],
    index: Any,
    *,
    embed_rank: Callable[[np.ndarray], Sequence[str]],
    server: Sequence[Sequence[str]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(보고서, 행별 결과).

    vectors는 질의 종류(VARIANTS) → 행 순서의 질의 임베딩, index는 BM25 색인(`search(text)`),
    embed_rank는 벡터 → 조각 임베딩의 섹션 순위(영어 생성 질문의 왕복 필터), server는 행마다 현재
    서버 방식(`retrieve`, 영어 질의는 조각 임베딩 순위)의 섹션 순위다(없으면 그 줄을 내지 않는다).
    """
    lines = qindex.lines
    kinds = [line["kind"] for line in lines]
    sections = [line["section_id"] for line in lines]
    tags = [f"{line['kind']}_{line['lang']}" for line in lines]

    def bm25(text: str) -> list[str]:
        return [sid for sid, _score in index.search(text)]

    def own_rank(i: int) -> Sequence[str]:  # 한국어는 BM25, 영어는 조각 임베딩 순위
        if lines[i]["lang"] == "en":
            return embed_rank(qindex.matrix[i])
        return bm25(lines[i]["question"])

    keep = roundtrip_keep(range(len(lines)), sections, own_rank)
    pos = [i for i, kind in enumerate(kinds) if kind == "answerable"]
    kept = [i for i in pos if keep[i]]
    before = Counter(tags)
    after = Counter(tag for tag, ok in zip(tags, keep, strict=True) if ok)

    tops: dict[str, list[tuple[bool, bool]]] = {}
    out_rows = []
    for n, row in enumerate(rows):
        ranked = {"bm25": bm25(row.search), "rule_a": bm25(row.rule_a)}
        if server is not None:
            ranked["server"] = list(server[n])
        signals = {}
        for v, tag in zip(VARIANTS, ("", "_last"), strict=True):
            query = vectors[v][n]
            sims = (qindex.matrix @ _unit_rows(np.asarray(query, dtype=np.float64))).tolist()
            signals[v] = oom_signals(query, qindex.matrix, kinds, sections)
            for idx, name in ((pos, "qindex"), (kept, "qindex_roundtrip")):
                ranked[name + tag] = section_ranking(
                    [sims[i] for i in idx], [sections[i] for i in idx]
                )
            ranked["rrf" + tag] = rrf(ranked["bm25"], ranked["qindex" + tag])
            ranked["rrf_roundtrip" + tag] = rrf(ranked["bm25"], ranked["qindex_roundtrip" + tag])
        out_rows.append(
            {
                "id": row.id,
                "fold": row.fold,
                "category": row.category,
                "action": row.action,
                "english": row.english,
                "gold": list(row.gold),
                "top1": {name: r[0] if r else None for name, r in ranked.items()},
                "signals": signals,
            }
        )
        if row.gold:
            for name, r in ranked.items():
                tops.setdefault(name, []).append(_hit(row.gold, r))
            a, b = _hit(row.gold, ranked["bm25"]), _hit(row.gold, ranked["rule_a"])
            tops.setdefault("bm25_or_rule_a", []).append((a[0] or b[0], a[1] or b[1]))
    gold_rows = [row for row in rows if row.gold]

    oom: dict[str, Any] = {}
    in_auc = [i for i, row in enumerate(rows) if row.category in ("out_of_material", "normal")]
    is_oom = [row.category == "out_of_material" for row in rows]
    normal, folds = [row.category == "normal" for row in rows], [row.fold for row in rows]
    for v in VARIANTS:
        oom[v] = {}
        for name, sign in SIGNALS.items():
            scores = [sign * out["signals"][v][name] for out in out_rows]
            flags = fold_flags(scores, normal, folds)
            for out, flag in zip(out_rows, flags, strict=True):
                out.setdefault("flag", {}).setdefault(v, {})[name] = flag
            oom[v][name] = {
                "direction": sign,
                "auc": auc(
                    [scores[i] for i in in_auc if is_oom[i]],
                    [scores[i] for i in in_auc if not is_oom[i]],
                ),
                "ci95": bootstrap_auc(
                    [scores[i] for i in in_auc],
                    [is_oom[i] for i in in_auc],
                    [rows[i].cluster for i in in_auc],
                ),
                "caught": sum(f and o for f, o in zip(flags, is_oom, strict=True)),
                "of": sum(is_oom),
                "flagged_answer": sum(
                    f and row.action == "answer" for f, row in zip(flags, rows, strict=True)
                ),
                "flagged": sum(flags),
            }
    report = {
        "prompt_versions": sorted({line.get("prompt_version", "") for line in lines}),
        "embedding_model": qindex.model,
        "rows": len(rows),
        "rows_with_gold": len(gold_rows),
        "categories": dict(Counter(row.category for row in rows)),
        "questions": {
            "chunks": len({line["chunk_id"] for line in lines}),
            "before": dict(before),  # kind_lang → 질문 수
            "roundtrip_kept": {tag: after[tag] for tag in before},  # 왕복 필터 뒤
        },
        "max_fpr": MAX_FPR,
        "retrieval": {name: _hit_table(gold_rows, hits) for name, hits in tops.items()},
        "oom": oom,
    }
    return report, out_rows


def show(report: Mapping[str, Any], out: TextIO) -> None:
    q = report["questions"]
    out.write(f"개발 행 {report['rows']}개(정답 섹션 있음 {report['rows_with_gold']}개")
    out.write(f", 빈 질의라 건너뛴 행 {report.get('skipped_blank', 0)}개)")
    out.write(f" · 조각 {q['chunks']}개의 질문 {sum(q['before'].values())}개\n")
    kept = ", ".join(f"{tag} {q['roundtrip_kept'][tag]}/{n}" for tag, n in q["before"].items())
    out.write(f"왕복 필터 뒤 남은 질문(남은 수/전체): {kept}\n")
    out.write("검색 적중(1위/5위/행 수). 전체 | 그룹별 1위\n")
    for name, table in report["retrieval"].items():
        total = table["all"]
        parts = [f"{g} {t['hit1']}/{t['n']}" for g, t in table.items() if g != "all"]
        out.write(f"- {name}: {total['hit1']}/{total['hit5']}/{total['n']} | {', '.join(parts)}\n")
    out.write(f"자료 밖 신호(out_of_material 대 normal, 문턱: normal 오탐 {report['max_fpr']:.0%}")
    out.write(" 이하)\n")
    for variant, signals in report["oom"].items():
        for name, s in signals.items():
            ci = s["ci95"]
            auc_text = "없음" if s["auc"] is None else f"{s['auc']:.3f}"
            ci_text = "" if ci is None else f" [{ci[0]:.3f}, {ci[1]:.3f}]"
            out.write(f"- {variant}.{name}(방향 {s['direction']:+d}): AUC {auc_text}{ci_text}")
            out.write(f" · 잡은 자료 밖 {s['caught']}/{s['of']}")
            out.write(f" · 잘못 잡은 answer {s['flagged_answer']}(잡은 행 {s['flagged']})\n")
