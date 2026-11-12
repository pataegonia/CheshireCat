"""KB 섹션 읽기와 조각 내기(TU-03, 가드 GR-03과 같은 방식).

섹션 = `kb/<과목>/<ID>.md` 하나와 같은 이름의 CSV(`<ID>-table*.csv`)다. HTML은 읽지 않는다.
조각은 섹션 원문(md + CSV)을 `## `로 시작하는 줄 앞에서 자른 것이다(가드 R13 `kb_chunks`와 같다).
"""

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# 채점기 scoring.normalize와 같은 정규화: NFKC·소문자, 공백과 아래 문장부호 제거(테스트가 맞춰 본다)
_PUNCT: Final = frozenset(".,!?;:'\"“”‘’`()[]{}<>«»·…~-–—/\\")
_CHUNK_SPLIT: Final = re.compile(r"\n(?=## )")


def normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).lower()
    return "".join(ch for ch in folded if not ch.isspace() and ch not in _PUNCT)


def bigrams(text: str) -> list[str]:
    """정규화한 글의 문자 2-gram(겹쳐서 모두)."""
    s = normalize(text)
    return [s[i : i + 2] for i in range(len(s) - 1)]


@dataclass(frozen=True)
class Section:
    """교재 섹션 하나. 통합 계약 `Section`(IN-01)과 필드가 같다.

    계약이 main에 들어오면 그쪽을 쓴다.
    """

    id: str
    subject: str  # 과목 폴더 이름(python·prompt·finance)
    title: str
    text: str  # md 원문
    tables: tuple[str, ...] = ()  # 같은 이름의 CSV 원문
    names: tuple[str, ...] = ()  # 이름 사전(TU-04)이 채운다

    def full_text(self) -> str:
        """근거 묶음에 넣는 섹션 원문 전체: md 뒤에 CSV를 붙인다(가드 R13 `load_kb`와 같은 이음)."""
        return "\n\n".join([self.text, *self.tables])


def _title(sid: str, text: str) -> str:
    """첫 줄 `# <ID> <제목>`에서 제목만. 형식이 다르면 빈 문자열."""
    first = text.split("\n", 1)[0]
    if not first.startswith("# "):
        return ""
    title = first[2:].strip()
    return title[len(sid) :].strip() if title.startswith(sid) else title


def load_sections(kb_dir: Path) -> dict[str, Section]:
    """`kb_dir/<과목>/*.md`를 경로 순서대로 읽는다. 같은 이름의 CSV는 그 섹션의 표로 붙인다."""
    sections: dict[str, Section] = {}
    for path in sorted(kb_dir.glob("*/*.md")):
        text = path.read_text(encoding="utf-8")
        csvs = sorted(path.parent.glob(f"{path.stem}-table*.csv"))
        tables = tuple(csv.read_text(encoding="utf-8") for csv in csvs)
        sections[path.stem] = Section(
            path.stem, path.parent.name, _title(path.stem, text), text, tables
        )
    return sections


def split_chunks(text: str) -> list[str]:
    """`## `로 시작하는 줄마다 자른다. 코드 블록 안도 자르고, 첫 `## ` 앞부분은 따로 한 조각이다."""
    return _CHUNK_SPLIT.split(text)
