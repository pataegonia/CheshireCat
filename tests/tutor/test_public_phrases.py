"""TD-10·SPEC §8: 공개 세트 대화 문구를 서버 텍스트 자산에 적지 않는다(#139 리뷰).

서버가 읽거나 실행하는 튜터 텍스트(코드·프롬프트·응답 스키마·공유 설정)를 공개 세트 대화와 15자
창으로 대조한다. 공백을 지우고 비교하므로 띄어쓰기·줄바꿈을 바꿔 옮긴 문구도 잡는다.

- 대조하는 공개 문구: 공개 세트 400행(가드·튜터)의 대화 메시지 전부(학습자 발화와 앞 대화의 튜터
  발화). 판단 예시(TU-08)는 이 파일들을 기동 때 읽어 쓰므로 서버 코드에 문구를 적을 이유가 없다.
- 정답 답변(`reference_answer`)·가드 후보 답변은 대조하지 않는다. 고정 문장(`templates.py`)과
  프롬프트가 §3 답변 방식을 따르면서 정답 답변과 같은 문구를 일부 쓰고 있어, 그 범위는 따로 정한다.
"""

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PUBLIC = REPO / "competition/public"
WINDOW = 15

# 서버 경로가 아닌 것: 평가 도구·개발용(TD-09)·문서
_NOT_SERVER = {"eval", "dev_only", "docs", "__pycache__"}
_SUFFIXES = {".py", ".md", ".json", ".yaml"}
# 반드시 대조 대상에 들어 있어야 하는 자산(경로 규칙이 바뀌어 빠지면 테스트가 헛돈다)
_MUST_SCAN = {
    "tutor/judge/examples.py",
    "tutor/judge/signals.py",
    "tutor/prompts/main_call.md",
    "tutor/prompts/main_call.en.md",  # 영어 번역본(T3.2, `toggles.prompt_lang: en`)
    "tutor/answer/templates.py",
    "tutor/contract/tutor.schema.json",
    "config/actions.yaml",
    "config/tutor.yaml",
}
# 공개 발화와 서버 코드가 같이 쓰는 일반 문법 이름(KB 코드 예제를 묻는 발화). 공개 문구가 아니다
_GENERIC = ("raise ValueError",)


def _squeeze(text: str) -> str:
    return "".join(text.split())


def server_assets() -> dict[str, str]:
    """서버 텍스트 자산: `tutor/` 아래(평가·개발용·문서 제외)와 루트 `config/`. 경로 → 본문."""
    paths = [
        p
        for p in (REPO / "tutor").rglob("*")
        if p.is_file()
        and p.suffix in _SUFFIXES
        and not _NOT_SERVER & set(p.relative_to(REPO / "tutor").parts)
        and p.name not in {"CLAUDE.md", "pyproject.toml"}
    ]
    paths += sorted((REPO / "config").glob("*.yaml"))
    return {p.relative_to(REPO).as_posix(): p.read_text(encoding="utf-8") for p in paths}


def public_messages() -> list[str]:
    """공개 세트 대화 메시지 본문(학습자·튜터 발화). 정답 답변·후보 답변은 넣지 않는다."""
    texts = []
    for name in ("guard.jsonl", "tutor.jsonl"):
        with (PUBLIC / name).open(encoding="utf-8") as f:
            for line in f:
                texts += [m["content"] for m in json.loads(line)["messages"]]
    return texts


def overlaps(phrases: list[str], assets: dict[str, str]) -> list[tuple[str, str]]:
    """공개 문구와 자산이 공백을 지운 뒤 `WINDOW`자 이상 이어서 같은 곳(겹친 문자열, 자산 경로)."""
    where: dict[str, str] = {}
    for path, text in assets.items():
        s = _squeeze(text)
        for i in range(len(s) - WINDOW + 1):
            where.setdefault(s[i : i + WINDOW], path)
    found: dict[str, str] = {}
    for phrase in phrases:
        for generic in _GENERIC:
            phrase = phrase.replace(generic, "\0")
        s = _squeeze(phrase)
        for i in range(len(s) - WINDOW + 1):
            w = s[i : i + WINDOW]
            if w in where:
                found.setdefault(w, where[w])
    return sorted(found.items())


@pytest.fixture(scope="module")
def assets():
    return server_assets()


@pytest.fixture(scope="module")
def phrases():
    return public_messages()


def test_scans_every_server_asset_and_the_whole_public_set(assets, phrases):
    assert _MUST_SCAN <= set(assets)
    assert not [p for p in assets if p.startswith(("tutor/eval/", "tutor/dev_only/"))]
    rows = [line for n in ("guard.jsonl", "tutor.jsonl") for line in (PUBLIC / n).open("rb")]
    assert len(rows) == 400
    assert len(phrases) > 400  # 여러 턴 대화의 앞 발화까지 들어간다


def test_server_assets_have_no_public_set_phrases(assets, phrases):
    hits = overlaps(phrases, assets)
    shown = "\n".join(f"  {path}: {w!r}" for w, path in hits[:10])
    assert not hits, f"공개 세트 문구가 서버 자산에 있다({len(hits)}곳):\n{shown}"


def test_overlap_check_catches_a_copied_phrase_even_with_spacing_changed(phrases):
    # 대조가 실제로 잡는지: 공개 발화 한 줄을 띄어쓰기만 바꿔 자산에 넣으면 걸린다
    sample = next(p for p in phrases if len(_squeeze(p)) >= 40)
    planted = {"tutor/judge/planted.py": f'X = "{" ".join(_squeeze(sample))}"\n'}
    assert overlaps([sample], planted)
    assert not overlaps([sample], {"tutor/judge/clean.py": 'X = "리스트"\n'})


def test_generic_code_names_are_not_counted():
    asset = {"tutor/judge/x.py": "raise ValueError('bad')\n"}
    assert not overlaps(["과일 기록에서 raise ValueError는 왜 써요?"], asset)
