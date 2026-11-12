"""I1.1 루트 실행 환경: 루트 lock과 가드 lock이 같은 버전을 쓰는지 확인한다.

가드는 `guardrail/uv.lock`으로 자기 CI를 돌리고, 서버는 루트 `uv.lock`으로 뜬다. 두 lock이 같은
패키지를 다른 버전으로 잡으면 가드 CI가 통과한 코드가 서버에서는 다른 버전 위에서 돈다.
"""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _versions(lock: Path) -> dict[str, str]:
    packages = tomllib.loads(lock.read_text(encoding="utf-8"))["package"]
    return {p["name"]: p["version"] for p in packages}


def test_root_lock_matches_guard_lock():
    root = _versions(ROOT / "uv.lock")
    guard = _versions(ROOT / "guardrail" / "uv.lock")
    shared = root.keys() & guard.keys()
    different = {
        name: (root[name], guard[name]) for name in sorted(shared) if root[name] != guard[name]
    }
    assert different == {}, (
        "루트 uv.lock과 guardrail/uv.lock의 버전이 다르다(루트, 가드). "
        "guardrail/uv.lock을 루트에 복사한 뒤 `uv lock`으로 다시 맞춘다."
    )


def test_root_lock_has_every_guard_package():
    """가드가 쓰는 패키지는 전부 서버 환경에도 있어야 한다."""
    root = _versions(ROOT / "uv.lock")
    guard = _versions(ROOT / "guardrail" / "uv.lock")
    assert sorted(guard.keys() - root.keys()) == []


def test_server_packages_import_from_repo_root():
    """세 트랙 코드가 한 환경에서 함께 import된다."""
    import guardrail.server

    import integration.actions
    import integration.contracts
    import tutor

    assert callable(guardrail.server.create_app)
    assert tutor.router is not None
    assert integration.contracts.SECTION_IDS
    assert callable(integration.actions.finalize)
