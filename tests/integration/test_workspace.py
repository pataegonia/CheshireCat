"""I1.1 루트 실행 환경: uv workspace 하나, lock 하나(통합 PRD §5).

서버는 `/guard`와 `/tutor`가 한 프로세스로 뜬다. 그래서 세 트랙 코드가 같은 환경, 같은 lock 위에서
돌아야 한다. 가드 CI가 통과한 버전과 제출 서버가 쓰는 버전이 달라지지 않게 한다.
"""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _lock(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _versions(lock: dict) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for package in lock["package"]:
        found.setdefault(package["name"], set()).add(package["version"])
    return found


def test_guard_and_tutor_are_members_of_the_root_workspace():
    members = set(_lock(ROOT / "uv.lock")["manifest"]["members"])
    assert {"guardrail", "tutor"} <= members


def test_tutor_has_no_lock_of_its_own():
    assert not (ROOT / "tutor" / "uv.lock").exists()


def test_leftover_guard_lock_does_not_disagree_with_the_root_lock():
    """`guardrail/uv.lock`은 workspace로 합친 뒤에는 쓰이지 않는다.

    가드 쪽에서 지우기 전까지 남아 있을 수 있으므로, 남아 있는 동안에는 겹치는 패키지 버전이
    루트와 같은지 본다(지워지면 볼 것이 없다).
    """
    leftover = ROOT / "guardrail" / "uv.lock"
    if not leftover.exists():
        return
    root = _versions(_lock(ROOT / "uv.lock"))
    guard = _versions(_lock(leftover))
    different = {
        name: (sorted(root[name]), sorted(guard[name]))
        for name in sorted(root.keys() & guard.keys())
        if root[name] != guard[name]
    }
    assert different == {}, "루트 uv.lock과 남아 있는 guardrail/uv.lock의 버전이 다르다(루트, 가드)"


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
