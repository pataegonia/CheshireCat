"""R13 공통 로더: 공개 세트·KB·채점 함수. 실행은 `python -X utf8 <script>.py`."""

import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
COMP = ROOT / "competition"
sys.path.insert(0, str(COMP / "grader"))
import scoring  # noqa: E402  (exact grader code)

BLOCK_CATS = {"jailbreak_injection", "harmful_content", "academic_dishonesty", "off_topic"}


def read_jsonl(name):
    return [
        json.loads(l)
        for l in (COMP / "public" / name).read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]


def guard_rows():
    return read_jsonl("guard.jsonl")


def tutor_rows():
    return read_jsonl("tutor.jsonl")


def load_kb():
    """{section_id: text(md + tables)} — same join as harness/score_local.load_kb_dir."""
    kb = {}
    for p in sorted((COMP / "kb").glob("*/*.md")):
        tables = sorted(p.parent.glob(f"{p.stem}-table*.csv"))
        kb[p.stem] = "\n\n".join(
            [p.read_text(encoding="utf-8"), *(t.read_text(encoding="utf-8") for t in tables)]
        )
    return kb


def kb_chunks(kb):
    """Split each section at '## ' headings -> list of (section_id, heading, text)."""
    out = []
    for sid, text in kb.items():
        parts = re.split(r"\n(?=## )", text)
        for part in parts:
            head = part.splitlines()[0].lstrip("# ").strip() if part.strip() else ""
            out.append((sid, head, part))
    return out


def norm(s):
    return scoring.normalize(s)


def bigrams(s):
    s = norm(s)
    return [s[i : i + 2] for i in range(len(s) - 1)]


class BM25:
    """Tiny char-2gram BM25 (k1=1.2, b=0.75)."""

    def __init__(self, docs, k1=1.2, b=0.75):
        self.tfs = [Counter(bigrams(d)) for d in docs]
        self.lens = [sum(t.values()) for t in self.tfs]
        self.avg = sum(self.lens) / len(self.lens)
        df = Counter(g for t in self.tfs for g in t)
        n = len(docs)
        self.idf = {g: math.log(1 + (n - c + 0.5) / (c + 0.5)) for g, c in df.items()}
        self.k1, self.b = k1, b

    def scores(self, query):
        q = Counter(bigrams(query))
        res = []
        for tf, ln in zip(self.tfs, self.lens):
            s = 0.0
            for g, qc in q.items():
                f = tf.get(g)
                if f:
                    s += (
                        self.idf[g]
                        * f
                        * (self.k1 + 1)
                        / (f + self.k1 * (1 - self.b + self.b * ln / self.avg))
                    )
            res.append(s)
        return res


def last_user(r):
    return r["messages"][-1]["content"]


def all_user(r):
    return "\n".join(m["content"] for m in r["messages"] if m["role"] == "user")


def output_label(lab):
    if "grounded" not in lab:
        return None
    return "unsafe" if not lab["safe"] else ("grounded" if lab["grounded"] else "ungrounded")


def out_part(pred_grounded, pred_spans, pred_safe, label, gold_spans, gold_safe):
    """Exact per-item output component used in scoring.guard_score."""
    oc = float(pred_grounded == (label != "ungrounded"))
    return (oc + scoring.span_f1(pred_spans, gold_spans) + float(pred_safe == gold_safe)) / 3


assert scoring.span_f1([], []) == 1.0 and scoring.span_f1(["a"], []) == 0.0
