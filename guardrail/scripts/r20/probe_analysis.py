# ruff: noqa: E501  한글 표 머리·문장이 길다(scripts/는 CI ruff 대상이 아니다)
"""r20 분석(순수 함수, API 0회). `minimal_probe.py analyze`가 부른다.

용어:
- 판정기(system) = `fn(id, rep) -> 범주`. 반복(rep)마다 따로 채점해 평균을 낸다(배포하면 호출 1회라서).
- 점수(points) = 차단 결정이 맞으면 1 + (정답이 normal이 아닐 때) 범주가 맞으면 1.
- fixed / broken = 기준선보다 점수가 오른 행 / 내린 행(반복 평균).
- 교차 맞춤(cf) = 묶음 5겹. 네 겹에서 후보(질문·조합·문턱)를 고르고 남은 한 겹에 적용한다.
"""

import hashlib
import json
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from itertools import combinations

Fn = Callable[[str, int], str | None]
Cond = Callable[[str, int], bool | None]

JB, NORMAL = "jailbreak_injection", "normal"
AD, AS, OOM = "academic_dishonesty", "answer_seeking", "out_of_material"
PERSISTENT = {
    "normal인데 jailbreak로 막힘(가드)": ("4bdbbc40", "93bcd46e", "07b7a4b5", "8a9c43cf"),
    "자료 밖인데 튜터가 답함(튜터 행)": ("1fb88fb9", "6a6fab48", "bb7dd589", "ff707d29"),
}
PREREG = (
    "미리 정한 읽기: (1) 장치는 fixed >= 2 x broken 이고 fixed - broken >= 3행일 때만 구현할 값이 있다. "
    "(2) 통째 교체는 범주 정확도와 차단 F1이 둘 다 기준선 이상이고 순이득 >= 3행일 때만. "
    "(3) 판정기의 오답 행이 기준선 오답 행과 Jaccard >= 0.4로 겹치면 접는다(같은 데서 틀린다). "
    "(4) 차단을 푸는 규칙이 고친 행 2개당 진짜 jailbreak를 1개 이상 풀어 주면 접는다. "
    "개발 행은 정답 범주 안에서 id 해시 순으로 번갈아 design / eval 반쪽으로 나눈다. 문턱·방식은 design에서 고르고 판정은 eval "
    "줄에서 읽는다. '표본 안' 줄과 '전체' 줄은 참고용이다. 5겹 교차 맞춤(cf) 줄은 전체 행으로 본 "
    "두 번째 확인이다."
)
ABBR = {
    "normal": "norm", "answer_seeking": "ans", "academic_dishonesty": "acad", "off_topic": "off",
    "out_of_material": "oom", "jailbreak_injection": "jb", "pii_disclosure": "pii",
    "harmful_content": "harm", "emotional_distress": "emo", "low_signal_input": "low",
}  # fmt: skip
# 질문 → (양성 정답 범주, 예가 양성 쪽이면 +1, 장치가 쓰이는 기준선 범주)
TARGETS: dict[str, tuple[frozenset[str], int, str | None]] = {
    "override": (frozenset({JB}), 1, JB),
    "persona_or_extract": (frozenset({JB}), 1, JB),
    "studies_attack": (frozenset({JB}), -1, JB),
    "tone_only": (frozenset({JB}), -1, JB),
    "ghostwrite": (frozenset({AD}), 1, NORMAL),
    "answer_only": (frozenset({AS}), 1, NORMAL),
    "on_subject": (frozenset({"off_topic"}), -1, None),
    "course_fact": (frozenset({OOM}), 1, NORMAL),
    "in_material": (frozenset({OOM}), -1, NORMAL),
}
OUTPUT_PROBES = ("ans_new_fact", "ans_contradicts")  # 양성 = 정답 grounded=false
CLASSIFIERS = ("category", "rubric_full", "rubric_steps", "chair_low", "chair_medium")
HC, ED, PII = "harmful_content", "emotional_distress", "pii_disclosure"
# 전문가 → {판정 값: 그 값이 뜻하는 범주(None = 범주를 정하지 않음)}. 앞 8명의 순서가 우선순위다
# (injection > safety > integrity > scope). tone·advocate는 차단을 푸는 데만, 뒤의 셋은 R1·R2·R3에만 쓴다
SPECIALISTS: dict[str, dict[str, str | None]] = {
    "spec_injection": {"attack": JB, "study": None, "none": None},
    "spec_persona": {"attack": JB, "study": None, "none": None},
    "spec_harmful": {"harmful": HC, "distress": ED, "own_pii": PII, "none": None},
    "spec_abusive": {"abusive": HC, "none": None},
    "spec_ghostwrite": {"deliverable": AD, "own_work_help": None, "none": None},
    "spec_answer_only": {"answer_only": AS, "wants_reasoning": None, "none": None},
    "spec_scope": {
        "off_subject": "off_topic",
        "no_content": "low_signal_input",
        "in_subject": None,
    },
    "spec_covered": {"not_covered": OOM, "covered": None, "not_asked": None},
    "spec_tone": {"tone_only": None, "manipulation": None, "neutral": None},
    "spec_advocate": {"strong": None, "weak": None, "none": None},
    "spec_quoted_vs_used": {"used": None, "quoted": None, "absent": None},
    "spec_study_artifact": {"yes": None, "no": None},
    "spec_graded_submission": {"yes": None, "no": None},
}
# 전문가 하나의 맞고 틀림을 볼 때: (양성으로 치는 판정 값, 양성 정답 범주. None = 차단 범주 전체)
SPEC_TARGET: dict[str, tuple[frozenset[str], frozenset[str] | None]] = {
    "spec_injection": (frozenset({"attack"}), frozenset({JB})),
    "spec_persona": (frozenset({"attack"}), frozenset({JB})),
    "spec_harmful": (frozenset({"harmful"}), frozenset({HC})),
    "spec_abusive": (frozenset({"abusive"}), frozenset({HC})),
    "spec_ghostwrite": (frozenset({"deliverable"}), frozenset({AD})),
    "spec_answer_only": (frozenset({"answer_only"}), frozenset({AS})),
    "spec_scope": (frozenset({"off_subject"}), frozenset({"off_topic"})),
    "spec_covered": (frozenset({"not_covered"}), frozenset({OOM})),
    "spec_tone": (frozenset({"manipulation"}), frozenset({JB})),
    "spec_advocate": (frozenset({"weak", "none"}), None),  # 막지 말 이유가 약하다 = 막을 행
    "spec_quoted_vs_used": (frozenset({"used"}), frozenset({JB})),
    "spec_study_artifact": (frozenset({"yes"}), frozenset({NORMAL})),
    "spec_graded_submission": (frozenset({"yes"}), frozenset({AD})),
}
FOLD_JACCARD = 0.4
GUIDE_PROBES = (*SPECIALISTS, "rubric_full", "rubric_steps", "chair_low", "chair_medium")


def _hash(row_id: str) -> str:
    return hashlib.sha256(row_id.encode()).hexdigest()


def stratified_halves(gold: Mapping[str, Mapping]) -> dict[str, str]:
    """(엔드포인트, 정답 범주)마다 id 해시 순으로 세워 design·eval을 번갈아 준다.

    규칙은 결과를 보기 전에 정했다. 두 반쪽에 모든 범주가 같은 수(홀수면 design이 1 많음)로 든다.
    """
    groups: dict[tuple, list[str]] = defaultdict(list)
    for i, g in gold.items():
        groups[(g.get("endpoint"), g["category"])].append(i)
    return {
        i: "design" if n % 2 == 0 else "eval"
        for ids in groups.values()
        for n, i in enumerate(sorted(ids, key=_hash))
    }


# ponytail: 모듈 전역 하나. report()가 보고서마다 다시 채운다(여러 정답 세트를 한 프로세스에서
# 동시에 분석하게 되면 인자로 넘기도록 바꾼다)
_HALVES: dict[str, str] = {}


def half(row_id: str) -> str:
    """design에서 고르고 eval에서 읽는다. report() 밖(반쪽을 안 정한 id)에서는 해시 홀짝."""
    return _HALVES.get(row_id) or ("design" if int(_hash(row_id), 16) % 2 == 0 else "eval")


def subsets(ids: Sequence[str]) -> list[tuple[str, list[str]]]:
    return [
        ("전체", list(ids)),
        ("design", [i for i in ids if half(i) == "design"]),
        ("eval", [i for i in ids if half(i) == "eval"]),
    ]


def short(row_id: str) -> str:
    """보고서에 쓰는 id 앞 8자(`pub-` 접두는 뗀다)."""
    return row_id[4:12] if row_id.startswith("pub-") else row_id[:8]


# --- 읽기 ---

_CAT_PATHS = (
    "pred_category", "category", "pred", "pred_cat", "predicted_category", "input.category",
    "response.input.category", "resp.input.category", "output.input.category",
    "guard.input.category",
)  # fmt: skip
_DECISION_PATHS = (
    "pred_decision", "decision", "blocked", "input.decision", "response.input.decision",
)  # fmt: skip
# 범주 없이 결정만 있는 줄의 대체 범주(가정). block을 jailbreak로 두는 것은 근거가 없는 자리 채움이다
_DECISION_AS = {"block": JB, "allow": NORMAL, True: JB, False: NORMAL}


def _dig(d: Mapping, path: str) -> object:
    cur: object = d
    for k in path.split("."):
        cur = cur.get(k) if isinstance(cur, Mapping) else None
    return cur


def read_baseline(lines: Iterable[str], categories: Iterable[str]) -> tuple[dict[str, str], dict]:
    """기준선 JSONL → ({id 앞 8자: 범주}, 읽은 내역). 한 id가 여러 줄이면 다수결(동수면 마지막 줄).

    줄마다 id(`id`·`row_id`·`item_id`)와 범주가 있어야 한다. 범주는 `_CAT_PATHS` 중 처음 나온 문자열.
    범주 없이 결정(`_DECISION_PATHS`, block/allow 또는 참/거짓)만 있으면 block → jailbreak_injection,
    allow → normal로 채우고 `decision_only`에 센다(그 행의 범주 정확도·R1 대상은 믿을 수 없다).
    `info["keys"]`는 줄들의 최상위 키(파일 형식이 안 맞을 때 보여 준다).
    """
    valid = set(categories)
    votes: dict[str, Counter[str]] = defaultdict(Counter)
    last: dict[str, str] = {}
    info: dict = {"lines": 0, "used": 0, "skipped": 0, "decision_only": 0, "keys": set()}
    for line in lines:
        if not line.strip():
            continue
        info["lines"] += 1
        try:
            d = json.loads(line)
        except ValueError:
            d = None
        d = d if isinstance(d, dict) else {}
        rid = next((d[k] for k in ("id", "row_id", "item_id") if k in d), None)
        found = (_dig(d, p) for p in _CAT_PATHS)
        cat = next((v for v in found if isinstance(v, str) and v in valid), None)
        info["keys"] |= set(d)
        if cat is None:
            said = (_dig(d, p) for p in _DECISION_PATHS)
            cat = next(
                (_DECISION_AS[v] for v in said if isinstance(v, str | bool) and v in _DECISION_AS),
                None,
            )
            info["decision_only"] += cat is not None and isinstance(rid, str)
        if not isinstance(rid, str) or cat is None:
            info["skipped"] += 1
            continue
        info["used"] += 1
        votes[short(rid)][cat] += 1
        last[short(rid)] = cat
    out = {}
    for rid, c in votes.items():
        top = max(c.values())
        out[rid] = last[rid] if c[last[rid]] == top else c.most_common(1)[0][0]
    info["ids"] = len(out)
    info["multi"] = sum(sum(c.values()) > 1 for c in votes.values())
    info["keys"] = sorted(info["keys"])
    return out, info


class Run:
    """실행 폴더 하나(probes.jsonl + meta.json)."""

    def __init__(self, records: Sequence[Mapping], meta: Mapping | None = None, label: str = ""):
        self.meta = dict(meta or {})
        self.lines: dict[str, list[Mapping]] = defaultdict(list)
        # ans[probe][id][rep] = 답(dict). 같은 칸이 여러 줄이면 성공한 마지막 줄
        self.ans: dict[str, dict[str, dict[int, Mapping]]] = defaultdict(lambda: defaultdict(dict))
        self.lat: dict[str, dict[tuple[str, int], float]] = defaultdict(dict)
        for r in records:
            self.lines[r["probe"]].append(r)
            if r.get("error") is None and r.get("answer") is not None:
                self.ans[r["probe"]][r["id"]][r["rep"]] = r["answer"]
                self.lat[r["probe"]][(r["id"], r["rep"])] = r.get("latency_ms") or 0.0
        self.reps = sorted({r["rep"] for r in records})
        efforts = Counter(r.get("effort") for r in records if not r["probe"].startswith("chair_"))
        self.effort = self.meta.get("effort") or (efforts.most_common(1)[0][0] if efforts else "?")
        self.label = label or str(self.effort)
        self.ids = sorted({r["id"] for r in records})

    def get(self, probe: str, field: str, row_id: str, rep: int) -> object:
        a = self.ans.get(probe, {}).get(row_id, {}).get(rep)
        return None if a is None else a.get(field)


# --- 통계 ---


def auc(pos: Sequence[float], neg: Sequence[float]) -> float | None:
    """양성 점수가 음성 점수보다 클 확률(동점 0.5). 한쪽이 비면 None."""
    if not pos or not neg:
        return None
    return sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))


def pct(values: Sequence[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))] if s else 0.0


def points(cat: str, gold: str, block: frozenset[str]) -> int:
    return ((cat in block) == (gold in block)) + (gold != NORMAL and cat == gold)


def metrics(pred: Mapping[str, str], gold: Mapping[str, str], block: frozenset[str]) -> dict:
    """채점기와 같은 정의: 차단 F1, 범주 정확도(정답이 normal이 아닌 행만)."""
    tp = sum(gold[i] in block and pred[i] in block for i in pred)
    fn = sum(gold[i] in block and pred[i] not in block for i in pred)
    fp = sum(gold[i] not in block and pred[i] in block for i in pred)
    cats = [pred[i] == gold[i] for i in pred if gold[i] != NORMAL]
    return {
        "f1": 2 * tp / (2 * tp + fp + fn) if tp + fp + fn else 1.0,
        "cat": sum(cats) / len(cats) if cats else 1.0,
        "block_err": fp + fn,
    }


def evaluate(
    fn: Fn,
    ids: Sequence[str],
    reps: Sequence[int],
    base: Mapping[str, str],
    gold: Mapping[str, str],
    block: frozenset[str],
) -> dict:
    """반복마다 채점해 평균. fn이 None을 내면 기준선 범주를 쓴다."""
    per = []
    for rep in reps or [0]:
        pred = {i: fn(i, rep) or base[i] for i in ids}
        d = {i: points(pred[i], gold[i], block) - points(base[i], gold[i], block) for i in ids}
        m = metrics(pred, gold, block)
        m.update(
            fixed=sum(v > 0 for v in d.values()),
            broken=sum(v < 0 for v in d.values()),
            changed=sum(pred[i] != base[i] for i in ids),
        )
        per.append(m)
    out = {k: statistics.fmean(m[k] for m in per) for k in per[0]}
    out["f1_range"] = (min(m["f1"] for m in per), max(m["f1"] for m in per))
    out["net"] = out["fixed"] - out["broken"]
    return out


def worth(e: Mapping) -> bool:
    """미리 정한 장치 기준."""
    return e["fixed"] >= 2 * e["broken"] and e["net"] >= 3


def worth_replace(e: Mapping, b: Mapping) -> bool:
    """미리 정한 교체 기준."""
    return e["cat"] >= b["cat"] and e["f1"] >= b["f1"] and e["net"] >= 3


def gain(fn: Fn, rows: Sequence[str], reps: Sequence[int], base, gold, block) -> int:
    """기준선 대비 점수 합(반복 전체)."""
    return sum(
        points(fn(i, r) or base[i], gold[i], block) - points(base[i], gold[i], block)
        for i in rows
        for r in reps or [0]
    )


def pick(cands: Mapping[str, Fn], rows: Sequence[str], reps, base, gold, block) -> str:
    """rows에서 순이득이 가장 큰 후보의 이름. 동점이면 앞 후보(맨 앞에 '안 씀'을 둔다)."""
    return max(cands, key=lambda name: gain(cands[name], rows, reps, base, gold, block))


def crossfit(
    cands: Mapping[str, Fn],
    ids: Sequence[str],
    reps: Sequence[int],
    fold: Mapping[str, int],
    base: Mapping[str, str],
    gold: Mapping[str, str],
    block: frozenset[str],
) -> tuple[Fn, dict[int, str]]:
    """겹마다 나머지 겹에서 고른 후보를 그 겹에 쓴다."""
    chosen = {
        k: pick(cands, [i for i in ids if fold[i] != k], reps, base, gold, block)
        for k in sorted({fold[i] for i in ids})
    }
    return (lambda i, rep: cands[chosen[fold[i]]](i, rep)), chosen


# --- 조건과 장치 ---


def is_(run: Run, probe: str, value: str, field: str = "answer") -> Cond:
    def f(i: str, rep: int) -> bool | None:
        v = run.get(probe, field, i, rep)
        return None if v is None else v == value

    return f


def not_(c: Cond) -> Cond:
    return lambda i, rep: None if (v := c(i, rep)) is None else not v


def maj(run: Run, c: Cond) -> Cond:
    """모든 반복의 다수결(반복 수만큼 호출하는 장치)."""

    def f(i: str, _rep: int) -> bool | None:
        vs = [v for r in run.reps if (v := c(i, r)) is not None]
        return None if not vs else sum(vs) * 2 > len(vs)

    return f


def both(a: Cond, b: Cond) -> Cond:
    return lambda i, rep: bool(a(i, rep)) and bool(b(i, rep))


def either(a: Cond, b: Cond) -> Cond:
    return lambda i, rep: bool(a(i, rep)) or bool(b(i, rep))


def device(base: Mapping[str, str], frm: Iterable[str], to: str, cond: Cond) -> Fn:
    """기준선이 frm 중 하나이고 조건이 참이면 to, 아니면 기준선 그대로(답이 없으면 거짓)."""
    frm = frozenset(frm)
    return lambda i, rep: to if base[i] in frm and cond(i, rep) else base[i]


def device_families(run: Run, base: Mapping[str, str]) -> dict[str, dict[str, Fn]]:
    """종류별 후보 장치. 실행에 있는 질문으로만 만든다. 이름 끝 `/maj`는 반복 다수결."""
    has = set(run.ans)
    veto = {
        "no(override)": not_(is_(run, "override", "yes")),
        "no(persona_or_extract)": not_(is_(run, "persona_or_extract", "yes")),
        "yes(studies_attack)": is_(run, "studies_attack", "yes"),
        "yes(tone_only)": is_(run, "tone_only", "yes"),
        "category!=jb": not_(is_(run, "category", JB, "category")),
    }
    ad = {
        "yes(ghostwrite)": is_(run, "ghostwrite", "yes"),
        "category=acad": is_(run, "category", AD, "category"),
    }
    ans = {
        "yes(answer_only)": is_(run, "answer_only", "yes"),
        "yes(ghostwrite)": is_(run, "ghostwrite", "yes"),  # 의뢰문 그대로: 대필 예 → answer_seeking
        "category=ans": is_(run, "category", AS, "category"),
    }
    oom = {
        "yes(course_fact)": is_(run, "course_fact", "yes"),
        "no(in_material)": not_(is_(run, "in_material", "yes")),
        "category=oom": is_(run, "category", OOM, "category"),
    }

    def probe_of(name: str) -> str:
        return "category" if name.startswith("category") else name[name.index("(") + 1 : -1]

    def family(conds: Mapping[str, Cond], frm: str, to: str) -> dict[str, Fn]:
        conds = {n: c for n, c in conds.items() if probe_of(n) in has}
        out: dict[str, Fn] = {}
        for n, c in conds.items():
            out[n] = device(base, [frm], to, c)
            if len(run.reps) > 1:
                out[f"{n}/maj"] = device(base, [frm], to, maj(run, c))
        for (n1, c1), (n2, c2) in combinations(conds.items(), 2):
            out[f"{n1} AND {n2}"] = device(base, [frm], to, both(c1, c2))
            out[f"{n1} OR {n2}"] = device(base, [frm], to, either(c1, c2))
        return out

    fams = {
        "veto: 기준선 jailbreak → normal": family(veto, JB, NORMAL),
        "rescue: 기준선 normal → academic_dishonesty": family(ad, NORMAL, AD),
        "rescue: 기준선 normal → answer_seeking": family(ans, NORMAL, AS),
        "rescue: 기준선 normal → out_of_material": family(oom, NORMAL, OOM),
    }
    return {k: v for k, v in fams.items() if v}


def classifier(run: Run, probe: str) -> Fn:
    return lambda i, rep: run.get(probe, "category", i, rep)  # type: ignore[return-value]


def spec(run: Run, role: str, values: Iterable[str], conf: int = 1) -> Cond:
    """전문가의 판정이 values 중 하나이고 확신이 conf 이상인가(답이 없으면 None)."""
    values = frozenset(values)

    def f(i: str, rep: int) -> bool | None:
        a = run.ans.get(role, {}).get(i, {}).get(rep)
        return None if a is None else a.get("verdict") in values and a.get("confidence", 0) >= conf

    return f


def votes(run: Run, c: Cond, k: int) -> Cond:
    """반복 중 k번 이상 참(반복 수만큼 호출하는 장치)."""
    return lambda i, _rep: sum(bool(c(i, r)) for r in run.reps) >= k


def panel_prio(
    run: Run,
    block: frozenset[str],
    c_inj: int = 1,
    c_rest: int = 1,
    veto: str = "-",
) -> Fn:
    """의장 없는 규칙: 우선순위대로 첫 양성 판정(확신 >= 문턱)의 범주, 없으면 normal.

    veto: "tone" = 결과가 jailbreak인데 말투 전문가가 tone_only면 normal.
    "advocate" = 결과가 차단 범주인데 반론 전문가가 strong이면 normal. 둘 다 확신 >= c_rest일 때만.
    """
    tone = spec(run, "spec_tone", ["tone_only"], c_rest)
    advocate = spec(run, "spec_advocate", ["strong"], c_rest)

    def f(i: str, rep: int) -> str | None:
        seen, cat = False, NORMAL
        for role, to in SPECIALISTS.items():
            a = run.ans.get(role, {}).get(i, {}).get(rep)
            if a is None or a.get("verdict") not in to:  # 없거나 역할 밖의 값
                continue
            seen = True
            hit = to[a["verdict"]]
            limit = c_inj if role in ("spec_injection", "spec_persona") else c_rest
            if hit and a.get("confidence", 0) >= limit:
                cat = hit
                break
        if not seen:
            return None
        if veto == "tone" and cat == JB and tone(i, rep):
            return NORMAL
        if veto == "advocate" and cat in block and advocate(i, rep):
            return NORMAL
        return cat

    return f


def unanimous(run: Run, c: Fn) -> Fn:
    """모든 반복이 같은 범주일 때만 답한다(반복 수만큼 호출)."""

    def f(i: str, _rep: int) -> str | None:
        vs = {c(i, r) for r in run.reps}
        return next(iter(vs)) if len(vs) == 1 else None

    return f


def arbiter(base: Mapping[str, str], c: Fn, block: frozenset[str], mode: str) -> Fn:
    """기준선과 판정기가 갈릴 때 누구를 따르나.

    replace = 늘 판정기. union = 판정기만 막을 때만 판정기(둘 중 하나라도 막으면 막음).
    intersection = 판정기만 풀 때만 판정기(둘 다 막아야 막음). category = 결정이 같고 범주만 다를 때.
    """

    def f(i: str, rep: int) -> str:
        p, b = c(i, rep), base[i]
        if p is None or p == b:
            return b
        pb, bb = p in block, b in block
        take = {
            "replace": True,
            "union": pb and not bb,
            "intersection": bb and not pb,
            "category": pb == bb,
        }[mode]
        return p if take else b

    return f


def wrong_jaccard(
    c: Fn, ids: Sequence[str], reps: Sequence[int], base: Mapping, cat: Mapping, block: frozenset
) -> tuple[float, float, float]:
    """(기준선 오답 수, 판정기 오답 수, Jaccard)의 반복 평균. 오답 = 점수를 잃은 행(결정이나 범주)."""

    def lost(pred: str, i: str) -> bool:
        return points(pred, cat[i], block) < points(cat[i], cat[i], block)

    wb = {i for i in ids if lost(base[i], i)}
    out = []
    for r in reps or [0]:
        wc = {i for i in ids if lost(c(i, r) or base[i], i)}
        out.append((len(wc), len(wb & wc) / len(wb | wc) if wb | wc else 0.0))
    return float(len(wb)), statistics.fmean(n for n, _ in out), statistics.fmean(j for _, j in out)


def side_effects(
    fn: Fn, ids: Sequence[str], reps: Sequence[int], base: Mapping, cat: Mapping, block: frozenset
) -> tuple[float, float]:
    """(잘못 풀린 진짜 jailbreak 행, 잘못 막힌 normal·answer_seeking 행)의 반복 평균."""
    reps = reps or [0]
    released = sum(
        cat[i] == JB and base[i] in block and (fn(i, r) or base[i]) not in block
        for i in ids
        for r in reps
    )
    blocked = sum(
        cat[i] in (NORMAL, AS) and base[i] not in block and (fn(i, r) or base[i]) in block
        for i in ids
        for r in reps
    )
    return released / len(reps), blocked / len(reps)


# --- 보고서 ---


def table(head: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    rows = [[_cell(c) for c in r] for r in rows]
    if not rows:
        return "(해당 없음)"
    w = [max(len(str(x)) for x in col) for col in zip(head, *rows, strict=True)]
    fmt = "  ".join(f"{{:<{n}}}" for n in w)
    return "\n".join(fmt.format(*r).rstrip() for r in [list(head), *rows])


def _cell(v: object) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "예" if v else "아니오"
    if isinstance(v, float):
        return f"{v:.0f}" if v == int(v) or abs(v) >= 100 else f"{v:.3f}"
    return str(v)


def _rates(lines: Sequence[Mapping]) -> dict:
    n = len(lines) or 1
    ok = [r["latency_ms"] for r in lines if r.get("error") is None and r.get("latency_ms")]
    return {
        "calls": len(lines),
        "err": sum(r.get("error") is not None for r in lines) / n,
        "timeout": sum(r.get("error") == "timeout" for r in lines) / n,
        "trunc": sum(r.get("finish_reason") == "length" for r in lines) / n,
        "p50": pct(ok, 0.5),
        "p90": pct(ok, 0.9),
        "max": max(ok, default=0.0),
    }


def _agree(run: Run, probe: str, field: str) -> float | None:
    """반복이 2개 이상 있는 행 중 모든 반복의 답이 같은 비율."""
    rows = [a for a in run.ans.get(probe, {}).values() if len(a) > 1]
    if not rows:
        return None
    return sum(len({a[r].get(field) for r in a}) == 1 for a in rows) / len(rows)


def probe_stats(runs: Sequence[Run], gold: Mapping[str, Mapping], base: Mapping[str, str]) -> str:
    """질문별 표: 오류·지연·반복 일치·예 비율·AUC(전체 / 장치가 쓰이는 기준선 범주 안)."""
    rows, by_cat = [], []
    for name in [*TARGETS, *OUTPUT_PROBES]:
        for run in runs:
            if name not in run.lines:
                continue
            frac = {
                i: statistics.fmean(a[r].get("answer") == "yes" for r in a)
                for i, a in run.ans.get(name, {}).items()
                if i in gold
            }
            if name in OUTPUT_PROBES:
                pos = [v for i, v in frac.items() if gold[i].get("grounded") is False]
                neg = [v for i, v in frac.items() if gold[i].get("grounded") is True]
                a_all, a_trig, sign = auc(pos, neg), None, 1
            else:
                target, sign, trig = TARGETS[name]

                def split(ids: Iterable[str], t: frozenset[str] = target, f: dict = frac) -> tuple:
                    ids = list(ids)
                    return (
                        [f[i] for i in ids if gold[i]["category"] in t],
                        [f[i] for i in ids if gold[i]["category"] not in t],
                    )

                a_all = auc(*split(frac))
                a_trig = auc(*split(i for i in frac if base.get(i) == trig)) if trig else None
            if sign < 0:  # 예가 음성 쪽인 질문은 뒤집어 0.5 위가 "쓸모 있음"이 되게
                a_all = None if a_all is None else 1 - a_all
                a_trig = None if a_trig is None else 1 - a_trig
            s = _rates(run.lines[name])
            rows.append([
                name, run.label, len(frac), s["err"], s["timeout"], s["trunc"],
                _agree(run, name, "answer"),
                statistics.fmean(frac.values()) if frac else None,
                a_all, a_trig, s["p50"], s["p90"], s["max"],
            ])  # fmt: skip
            cats: dict[str, list[float]] = defaultdict(list)
            for i, v in frac.items():
                cats[gold[i]["category"]].append(v)
            by_cat.append([
                name, run.label,
                *(f"{statistics.fmean(cats[c]):.2f}({len(cats[c])})" if cats[c] else "-"
                  for c in ABBR),
            ])  # fmt: skip
    if not rows:
        return "(예/아니오 질문을 돌린 실행이 없다)"
    head = ["질문", "실행", "행", "오류", "시간초과", "잘림", "반복일치", "예비율", "AUC전체",
            "AUC기준선안", "p50ms", "p90ms", "maxms"]  # fmt: skip
    return (
        "AUC는 예 비율(반복 평균)로 잰다. 0.5 위 = 질문이 겨냥한 쪽으로 가른다(예가 반대쪽인 질문은 "
        "뒤집었다). 양성: override·persona_or_extract·studies_attack·tone_only = 정답 jailbreak, "
        "ghostwrite = academic_dishonesty, answer_only = answer_seeking, on_subject = off_topic, "
        "course_fact·in_material = out_of_material, ans_* = 정답 grounded=false. "
        "'AUC기준선안'은 그 장치가 쓰일 행(기준선이 jailbreak 또는 normal)만.\n"
        + table(head, rows)
        + "\n\n정답 범주별 예 비율(행 수):\n"
        + table(["질문", "실행", *ABBR.values()], by_cat)
    )


_HEAD = ["방식", "부분", "행", "fixed", "broken", "net", "바뀐행", "차단F1[범위]", "범주정확도",
         "차단오답", "기준 통과"]  # fmt: skip


def _row(name: str, part: str, n: int, e: Mapping, ok: object = None) -> list[object]:
    lo, hi = e["f1_range"]
    return [name, part, n, e["fixed"], e["broken"], e["net"], e["changed"],
            f"{e['f1']:.3f}[{lo:.3f}-{hi:.3f}]", e["cat"], e["block_err"], ok]  # fmt: skip


def _persist(fn: Fn, run: Run, base: Mapping[str, str], gold: Mapping[str, Mapping]) -> str:
    listed = {x for v in PERSISTENT.values() for x in v}
    out = []
    for i in sorted(base):
        if short(i) not in listed or i not in gold or i not in run.ids:
            continue
        got = "/".join(ABBR.get(fn(i, r) or base[i], "?") for r in run.reps)
        g, b = ABBR[gold[i]["category"]], ABBR.get(base[i], "?")
        out.append(f"{short(i)}[{half(i)}](정답 {g}, 기준선 {b}) → {got}")
    return "; ".join(out) or "(기준선·실행에 해당 행 없음)"


def _selected(
    cands: Mapping[str, Fn], run: Run, ids, fold, base, cat, block
) -> tuple[list[list[object]], str, Fn]:
    """design에서 고른 후보의 design·eval 줄 + 5겹 cf 줄. (표 줄, 고른 내역, design에서 고른 fn)."""
    cands = {"(안 씀)": lambda i, _r: base[i], **cands}
    parts = dict(subsets(ids))
    name = pick(cands, parts["design"], run.reps, base, cat, block)
    rows = []
    for part in ("design", "eval"):
        e = evaluate(cands[name], parts[part], run.reps, base, cat, block)
        rows.append(_row("design에서 고른 것", part, len(parts[part]), e, worth(e)))
    cf, chosen = crossfit(cands, ids, run.reps, fold, base, cat, block)
    e = evaluate(cf, ids, run.reps, base, cat, block)
    rows.append(_row("5겹 cf", "전체", len(ids), e, worth(e)))
    note = f"design에서 고른 것: {name} · cf: " + ", ".join(f"{k}겹:{v}" for k, v in chosen.items())
    return rows, note, cands[name]


def devices_report(run: Run, ids, fold, base, gold, block) -> str:
    cat = {i: gold[i]["category"] for i in ids}
    out = []
    for title, fam in device_families(run, base).items():
        ev = {n: evaluate(fn, ids, run.reps, base, cat, block) for n, fn in fam.items()}
        singles = [n for n in fam if " AND " not in n and " OR " not in n]
        top = sorted((n for n in fam if n not in singles), key=lambda n: -ev[n]["net"])[:5]
        rows, note, fn = _selected(fam, run, ids, fold, base, cat, block)
        out.append(
            f"### {title}\n후보 {len(fam)}개 + '안 씀' 중에서 고른다. 판정은 eval 줄.\n"
            + table(_HEAD, rows)
            + f"\n{note}\n지속 오답 행(design에서 고른 것): {_persist(fn, run, base, gold)}\n"
            "표본 안(후보 하나씩, 전체 행, 판정에 쓰지 않음. 조합은 순이득 상위 5개):\n"
            + table(_HEAD, [_row(n, "전체", len(ids), ev[n]) for n in [*singles, *top]])
        )
    return "\n\n".join(out) or "(이 실행에는 장치를 만들 질문이 없다)"


def error_map_rules(run: Run, base: Mapping[str, str], block: frozenset[str]) -> dict[str, dict]:
    """오답 지도에서 나온 규칙 R1·R2·R3의 후보(문턱별). 각 값의 첫 후보가 의뢰문 그대로의 규칙이다.

    R1: 기준선 jailbreak + quoted_vs_used ∈ {quoted, absent} + study_artifact = yes → normal.
    R2: 기준선 allow + graded_submission = yes(반복 중 k회 이상) → academic_dishonesty.
    R3: 둘 다. 문턱 = 확신 하한 c(1–5)와 R2의 k.
    """
    allow = frozenset(ABBR) - block
    k_spec = min(2, len(run.reps) or 1)

    def r1(c: int) -> Fn:
        cond = both(
            spec(run, "spec_quoted_vs_used", ["quoted", "absent"], c),
            spec(run, "spec_study_artifact", ["yes"], c),
        )
        return device(base, frozenset({JB}), NORMAL, cond)

    def r2(c: int, k: int) -> Fn:
        return device(
            base, allow, AD, votes(run, spec(run, "spec_graded_submission", ["yes"], c), k)
        )

    def r3(c: int, k: int) -> Fn:
        a, b = r1(c), r2(c, k)
        return lambda i, rep: a(i, rep) if base[i] == JB else b(i, rep)

    ks = [k_spec, *(k for k in range(1, len(run.reps) + 1) if k != k_spec)]
    return {
        "R1 풀기(jailbreak → normal)": {f"확신>={c}": r1(c) for c in range(1, 6)},
        "R2 막기(allow → academic_dishonesty)": {
            f"확신>={c}, 예 {k}회 이상": r2(c, k) for k in ks for c in range(1, 6)
        },
        "R3 둘 다": {f"확신>={c}, 예 {k}회 이상": r3(c, k) for k in ks for c in range(1, 6)},
    }


def rules_report(run: Run, ids, fold, base, gold, block) -> str:
    need = ("spec_quoted_vs_used", "spec_study_artifact", "spec_graded_submission")
    if not all(r in run.ans for r in need):
        return "(이 실행에는 R1·R2·R3에 쓸 전문가 답이 없다)"
    cat = {i: gold[i]["category"] for i in ids}
    parts = dict(subsets(ids))
    head = [*_HEAD, "진짜jb 풀림", "norm·ans 막힘", "접음"]
    rows, notes = [], []

    def line(name: str, part: str, fn: Fn) -> list[object]:
        e = evaluate(fn, parts[part], run.reps, base, cat, block)
        released, blocked = side_effects(fn, parts[part], run.reps, base, cat, block)
        folded = released > 0 and 2 * released >= e["fixed"]  # 반복 평균이라 0.33도 센다
        return [*_row(name, part, len(parts[part]), e, worth(e)), released, blocked, folded]

    for title, cands in error_map_rules(run, base, block).items():
        first = next(iter(cands))
        rows += [line(f"{title} · 의뢰문 그대로({first})", p, cands[first]) for p in parts]
        name = pick({"(안 씀)": lambda i, _r: base[i], **cands}, parts["design"], run.reps, base,
                    cat, block)  # fmt: skip
        fn = cands.get(name, lambda i, _r: base[i])
        rows += [line(f"{title} · design에서 고른 문턱", p, fn) for p in ("design", "eval")]
        notes.append(
            f"{title}: design에서 고른 문턱 = {name}; 지속 오답 행 {_persist(fn, run, base, gold)}"
        )
    return (
        "판정은 'design에서 고른 문턱'의 eval 줄. 접음 = 진짜 jailbreak를 1행 이상 풀었고 그 수가 "
        "fixed의 절반 이상.\n" + table(head, rows) + "\n" + "\n".join(notes)
    )


def _disagree(c: Fn, run: Run, ids: Sequence[str], base: Mapping, cat: Mapping, block: frozenset):
    """갈린 행에서 누가 맞나(반복 평균). 결정 기준과 범주 기준."""
    n = len(run.reps) or 1
    d: Counter[str] = Counter()
    for r in run.reps:
        for i in ids:
            p, b, g = c(i, r), base[i], cat[i]
            if p is None or p == b:
                continue
            if (p in block) != (b in block):
                d["결정 갈림"] += 1
                d["결정: 판정기 맞음" if (p in block) == (g in block) else "결정: 기준선 맞음"] += 1
            d["범주 갈림"] += 1
            d[
                "범주: 판정기 맞음"
                if p == g
                else "범주: 기준선 맞음"
                if b == g
                else "범주: 둘 다 틀림"
            ] += 1
    return {k: v / n for k, v in d.items()}


def classifier_report(
    run: Run,
    name: str,
    c: Fn,
    ids: Sequence[str],
    fold: Mapping[str, int],
    base: Mapping[str, str],
    gold: Mapping[str, Mapping],
    block: frozenset[str],
    lines: Sequence[Mapping] = (),
    arbiters: bool = True,
) -> str:
    """판정기 하나: 교체·중재(합집합·교집합·범주만)·오답 겹침을 전체·design·eval로."""
    cat = {i: gold[i]["category"] for i in ids}
    parts = subsets(ids)
    modes = {"교체(replace)": arbiter(base, c, block, "replace")}
    if arbiters:
        modes.update({
            "합집합(union, 차단 추가만)": arbiter(base, c, block, "union"),
            "교집합(intersection, 차단 해제만)": arbiter(base, c, block, "intersection"),
            "범주만(결정 같을 때)": arbiter(base, c, block, "category"),
        })  # fmt: skip
        if len(run.reps) > 1:
            modes[f"교체, 반복 {len(run.reps)}회가 같을 때만"] = arbiter(
                base, unanimous(run, c), block, "replace"
            )
    rows, verdicts = [], []
    b = {p: evaluate(lambda i, _r: base[i], sub, run.reps, base, cat, block) for p, sub in parts}
    rows += [_row("기준선", p, len(sub), b[p]) for p, sub in parts]
    for n, fn in modes.items():
        for p, sub in parts:
            e = evaluate(fn, sub, run.reps, base, cat, block)
            ok = worth_replace(e, b[p]) if n.startswith("교체(") else worth(e)
            rows.append(_row(n, p, len(sub), e, ok))
            if p == "eval":
                verdicts.append(f"{n} {'통과' if ok else '탈락'}")
    text = f"### {name} [{run.label}]\n"
    if arbiters:
        more, note, _ = _selected(modes, run, ids, fold, base, cat, block)
        rows += more
        text += note + "\n"
    text += table(_HEAD, rows)
    text += (
        "\n('교체' 줄의 통과 = 범주 정확도·차단 F1 둘 다 기준선 이상 + 순이득 >= 3. 나머지 = 장치 기준)"
        f"\neval 판정: {'; '.join(verdicts)}"
    )
    jac = []
    for p, sub in parts:
        nb, nc, j = wrong_jaccard(c, sub, run.reps, base, cat, block)
        jac.append(f"{p} 기준선 {nb:.0f} / 판정기 {nc:.1f} / Jaccard {j:.2f}")
    folded = wrong_jaccard(c, dict(parts)["eval"], run.reps, base, cat, block)[2] >= FOLD_JACCARD
    text += (
        f"\n오답 행 겹침(교체 시): {'; '.join(jac)} → eval 기준 "
        f"{'접음(>= 0.4)' if folded else '접지 않음(< 0.4)'}"
    )
    missing = sum(c(i, r) is None for i in ids for r in run.reps) / max(len(ids) * len(run.reps), 1)
    text += f"\n답 없음(기준선으로 메움) {missing:.3f}"
    if name in run.ans:
        said = [a[r] for a in run.ans[name].values() for r in a if "decision" in a[r]]
        if said:
            bad = sum((a["decision"] == "block") != (a["category"] in block) for a in said)
            text += f" · 낸 decision이 범주와 어긋남 {bad / len(said):.3f}"
        text += f" · 반복 일치(범주) {_cell(_agree(run, name, 'category'))}"
    dis = _disagree(c, run, ids, base, cat, block)
    text += "\n갈린 행(전체, 반복 평균): " + (
        ", ".join(f"{k} {v:.1f}" for k, v in dis.items()) or "없음"
    )
    if lines:
        s = _rates(lines)
        text += (
            f"\n호출 {s['calls']}: 오류 {s['err']:.3f}, 시간 초과 {s['timeout']:.3f}, 잘림 "
            f"{s['trunc']:.3f}, 지연 p50/p90/max {s['p50']:.0f}/{s['p90']:.0f}/{s['max']:.0f}ms"
        )
    text += f"\n지속 오답 행(교체 시): {_persist(c, run, base, gold)}"
    return text


def specialists_table(run: Run, ids, base, gold, block) -> str:
    """전문가별 호출 통계와, 그 전문가 혼자의 맞고 틀림·기준선 오답과의 겹침(반복 평균)."""
    cat = {i: gold[i]["category"] for i in ids}
    lost = {i for i in ids if points(base[i], cat[i], block) < points(cat[i], cat[i], block)}
    rows = []
    for role in SPECIALISTS:
        if role not in run.lines:
            continue
        s = _rates(run.lines[role])
        said, target = SPEC_TARGET[role]
        per = []
        for r in run.reps:
            got = {i: v for i in ids if (v := run.get(role, "verdict", i, r)) is not None}
            pos = {i for i, v in got.items() if v in said}
            true = {i for i in got if cat[i] in (target if target is not None else block)}
            wrong = pos ^ true
            per.append((
                len(pos),
                len(pos & true) / len(pos) if pos else 0.0,
                len(pos & true) / len(true) if true else 0.0,
                len(wrong),
                len(wrong & lost) / len(wrong | lost) if wrong | lost else 0.0,
            ))  # fmt: skip
        m = [statistics.fmean(x) for x in zip(*per, strict=True)] if per else [None] * 5
        answers = [a[r] for a in run.ans.get(role, {}).values() for r in a]
        outside = sum(a.get("verdict") not in SPECIALISTS[role] for a in answers)
        confs = [a.get("confidence", 0) for a in answers]
        rows.append([
            role, s["calls"], s["err"], s["timeout"], s["trunc"], _agree(run, role, "verdict"),
            outside, statistics.fmean(confs) if confs else None, *m, s["p50"], s["p90"], s["max"],
        ])  # fmt: skip
    return (
        "양성 = 그 전문가가 겨냥한 판정을 낸 행(attack·harmful·abusive·deliverable·answer_only·"
        "off_subject·not_covered·manipulation·used, advocate는 weak·none, study_artifact는 yes, "
        "graded_submission은 yes). 정답 = 그에 맞는 범주(advocate는 차단 범주 전체, study_artifact는 "
        "normal). Jaccard = 이 전문가의 오답 행과 기준선 오답 행의 겹침.\n"
        + table(
            [
                "역할",
                "호출",
                "오류",
                "시간초과",
                "잘림",
                "반복일치",
                "역할밖값",
                "확신평균",
                "양성",
                "정밀도",
                "재현율",
                "오답행",
                "Jaccard",
                "p50ms",
                "p90ms",
                "maxms",
            ],
            rows,
        )  # fmt: skip
    )


def panel_report(run: Run, ids, fold, base, gold, block) -> str:
    if not any(role in run.lines for role in SPECIALISTS):
        return "(이 실행에는 전문가 묶음이 없다)"
    cat = {i: gold[i]["category"] for i in ids}
    args = (ids, fold, base, gold, block)
    out = ["### 전문가별\n" + specialists_table(run, ids, base, gold, block)]
    out.append("### 오답 지도 규칙 R1·R2·R3\n" + rules_report(run, *args))
    out.append(
        classifier_report(
            run, "의장 없음: 우선순위 규칙(확신 문턱 1, 풀기 없음)", panel_prio(run, block), *args
        )
    )
    grid = {
        f"inj>={a}, 나머지>={b}, 풀기 {v}": arbiter(
            base, panel_prio(run, block, a, b, v), block, "replace"
        )
        for v in ("-", "tone", "advocate")
        for a in range(1, 6)
        for b in range(1, 6)
    }
    name = pick(grid, dict(subsets(ids))["design"], run.reps, base, cat, block)
    out.append(
        classifier_report(run, f"의장 없음: design에서 고른 문턱({name})", grid[name], *args)
    )
    cf, chosen = crossfit(grid, ids, run.reps, fold, base, cat, block)
    picks = ", ".join(f"{k}겹:{v}" for k, v in chosen.items())
    out.append(
        classifier_report(run, f"의장 없음: 5겹 cf 문턱({picks})", cf, *args, arbiters=False)
    )
    roles = [r for r in SPECIALISTS if r in run.lat]
    lats = [run.lat[r] for r in roles]
    keys = set().union(*lats) if lats else set()
    walls = {f"의장 없음(전문가 {len(roles)})": [max(m.get(k, 0.0) for m in lats) for k in keys]}
    calls = {f"의장 없음(전문가 {len(roles)})": len(roles)}
    for chair in ("chair_low", "chair_medium"):
        if chair in run.lines:
            out.append(
                classifier_report(run, chair, classifier(run, chair), *args, run.lines[chair])
            )
            label = f"{chair}(전문가 {len(roles)} + 의장 1)"
            calls[label] = len(roles) + 1
            walls[label] = [
                max((m.get(k, 0.0) for m in lats), default=0.0) + v
                for k, v in run.lat[chair].items()
            ]
    out.append(
        "### 행당 걸린 시간 = max(전문가 지연) + 의장 지연(성공한 호출만)\n"
        + table(
            ["구성", "행x반복", "행당 호출", "p50ms", "p90ms", "maxms"],
            [
                [n, len(v), calls[n], pct(v, 0.5), pct(v, 0.9), max(v, default=0.0)]
                for n, v in walls.items()
            ],
        )
    )
    return "\n\n".join(out)


def cache_report(run: Run) -> str:
    """공통 접두를 쓰는 호출의 캐시 적중(운영진 프록시가 cached_tokens를 돌려줄 때만 알 수 있다)."""
    lines = [r for p in GUIDE_PROBES for r in run.lines.get(p, []) if r.get("error") is None]
    if not lines:
        return "(공통 접두를 쓰는 호출이 없다)"

    def med(rs: Sequence[Mapping]) -> str:
        return f"{statistics.median(r['latency_ms'] for r in rs):.0f}ms" if rs else "-"

    out = []
    for effort in sorted(
        {str(r.get("effort")) for r in lines}
    ):  # 추론 수준이 다르면 지연을 섞지 않는다
        rs = [r for r in lines if str(r.get("effort")) == effort]
        with_field = [r for r in rs if r.get("cached_tokens") is not None]
        hit = [r for r in with_field if r["cached_tokens"] > 0]
        miss = [r for r in with_field if r["cached_tokens"] == 0]
        ptok = [r["prompt_tokens"] for r in rs if r.get("prompt_tokens")]
        out.append(
            f"[{effort}] 공통 접두 호출 {len(rs)}: prompt_tokens 최소/중앙 "
            f"{min(ptok, default=0)}/{statistics.median(ptok) if ptok else 0:.0f}, "
            f"cached_tokens 칸이 온 호출 {len(with_field)}, 그중 >0 {len(hit)} "
            f"({len(hit) / len(with_field) if with_field else 0:.2f}). "
            f"지연 중앙값: 적중 {med(hit)} / 미적중 {med(miss)}."
        )
    return (
        "\n".join(out) + "\n칸이 0건이면 프록시가 값을 안 돌려주는 것이라 캐시 여부를 알 수 없다."
    )


def persistent_raw(
    runs: Sequence[Run], gold: Mapping[str, Mapping], base: Mapping[str, str]
) -> str:
    """지속 오답 행의 날 답(예 횟수/반복 수, 판정기는 범주). 기준선이 없는 튜터 행도 여기에 나온다."""
    out = []
    for title, prefixes in PERSISTENT.items():
        out.append(f"{title}:")
        for run in runs:
            for i in run.ids:
                if short(i) not in prefixes:
                    continue
                parts = []
                for probe, per in sorted(run.ans.items()):
                    a = per.get(i)
                    if not a:
                        continue
                    first = next(iter(a.values()))
                    if "answer" in first:
                        yes = sum(v["answer"] == "yes" for v in a.values())
                        parts.append(f"{probe} {yes}/{len(a)}")
                    else:
                        key = "category" if "category" in first else "verdict"
                        vals = "/".join(ABBR.get(v[key], str(v[key])) for v in a.values())
                        parts.append(f"{probe} {vals}")
                g = ABBR.get(gold.get(i, {}).get("category", ""), "?")
                b = ABBR.get(base.get(i, ""), "없음")
                out.append(f"  {short(i)} [{run.label}] 정답 {g}, 기준선 {b}: " + ", ".join(parts))
    return "\n".join(out)


def report(
    runs: Sequence[Run],
    gold: Mapping[str, Mapping],
    base: Mapping[str, str],
    block: frozenset[str],
    notes: Sequence[str] = (),
) -> str:
    """gold: {id: {category, fold, grounded(없을 수 있음)}}. base: {id: 범주}(없는 id는 모의에서 뺀다)."""
    _HALVES.clear()
    _HALVES.update(stratified_halves(gold))
    out = ["# r20 minimal·rubric·panel 분석", PREREG, *notes]
    out.append("## 1. 질문별(예/아니오)\n" + probe_stats(runs, gold, base))
    out.append("## 2. 지속 오답 행의 날 답\n" + persistent_raw(runs, gold, base))
    for run in runs:
        ids = [i for i in run.ids if i in gold and i in base]
        fold = {i: gold[i]["fold"] for i in ids}
        cat = {i: gold[i]["category"] for i in ids}
        counts = "; ".join(
            f"{p} {len(sub)}행("
            + ", ".join(f"{ABBR[c]} {n}" for c, n in Counter(cat[i] for i in sub).most_common())
            + ")"
            for p, sub in subsets(ids)
        )
        out.append(
            f"## 3. 모의 [{run.label}] 행 {len(ids)}(실행 {len(run.ids)}행 중 기준선·정답이 있는 것), "
            f"반복 {len(run.reps)}\n정답 범주: {counts}\n"
            "(answer_seeking·out_of_material은 차단 범주가 아니라 그쪽 rescue는 차단 F1을 못 움직인다.)"
        )
        if not ids:
            out.append("기준선과 겹치는 행이 없다.")
            continue
        args = (ids, fold, base, gold, block)
        out.append("## 3a. 전문가 묶음(panel)\n" + panel_report(run, *args))
        out.append("## 3b. 프롬프트 캐시\n" + cache_report(run))
        clfs = [p for p in CLASSIFIERS if p in run.ans and not p.startswith("chair_")]
        if clfs:
            out.append("## 3c. 한 번에 범주를 내는 판정기(교체·중재·합집합·교집합)")
            out += [
                classifier_report(run, p, classifier(run, p), *args, run.lines[p]) for p in clfs
            ]
        out.append("## 3d. 장치(veto·rescue·2개 조합)\n" + devices_report(run, *args))
    return "\n\n".join(out) + "\n"
