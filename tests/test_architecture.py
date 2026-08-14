from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "src" / "dwp_agent"


def test_runtime_uses_installable_src_package() -> None:
    assert (PACKAGE_ROOT / "__init__.py").is_file()
    assert not list(ROOT.glob("*.py"))
    assert (PACKAGE_ROOT / "migrations" / "V1__create_agent_runtime_control_plane.sql").is_file()


def test_runtime_modules_stay_within_reviewable_size() -> None:
    oversized = {
        path.relative_to(ROOT): len(path.read_text(encoding="utf-8").splitlines())
        for path in PACKAGE_ROOT.glob("*.py")
        if len(path.read_text(encoding="utf-8").splitlines()) > 500
    }
    assert oversized == {}
