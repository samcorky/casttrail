import ast
from pathlib import Path

import pytest
from archunitpython import assert_passes, project_files

SRC_PATH = str(Path(__file__).resolve().parents[2] / "src" / "casttrail")


def test_no_circular_dependencies() -> None:
    """The application has no import cycles."""
    assert_passes(project_files(SRC_PATH).should().have_no_cycles())


def test_foundation_modules_do_not_depend_on_the_web_layer() -> None:
    """`config` and `db` are below `web` and `main`; they never import upwards."""
    for foundation in ("config.py", "db.py"):
        for upper in ("*web*", "*main.py"):
            rule = (
                project_files(SRC_PATH)
                .in_path(f"*{foundation}")
                .should_not()
                .depend_on_files()
                .in_path(upper)
            )
            assert_passes(rule)


def test_web_layer_does_not_import_the_composition_root() -> None:
    """`main` wires `web` together, not the other way round."""
    rule = (
        project_files(SRC_PATH)
        .in_folder("*web*")
        .should_not()
        .depend_on_files()
        .in_path("*main.py")
    )
    assert_passes(rule)


def test_persistence_does_not_depend_on_the_web_framework() -> None:
    """`db` stays usable from scripts and migrations without FastAPI."""
    rule = (
        project_files(SRC_PATH)
        .in_path("*db.py")
        .should_not()
        .depend_on_external_modules()
        .matching("fastapi*")
    )
    assert_passes(rule)


# Packages that hold plain service code. Rules on a package that does not exist yet
# pass without checking anything, and start to bite the day the package appears.
FRAMEWORK_FREE_PACKAGES = ("auth", "identity", "matching")


@pytest.mark.parametrize("package", FRAMEWORK_FREE_PACKAGES)
def test_service_packages_do_not_depend_on_the_web_layer_or_web_frameworks(
    package: str,
) -> None:
    """Service code is plain Python; cookies and routes live in `web`."""
    for upper in ("*web*", "*main.py"):
        rule = (
            project_files(SRC_PATH)
            .in_folder(f"*{package}*")
            .should_not()
            .depend_on_files()
            .in_path(upper)
        )
        assert_passes(rule)
    for framework in ("fastapi*", "starlette*"):
        framework_rule = (
            project_files(SRC_PATH)
            .in_folder(f"*{package}*")
            .should_not()
            .depend_on_external_modules()
            .matching(framework)
        )
        assert_passes(framework_rule)


def test_providers_return_data_and_never_touch_the_database() -> None:
    """Core owns persistence, identity, matching and notifications; providers do not."""
    for forbidden in ("*db.py", "*auth*", "*web*", "*main.py"):
        rule = (
            project_files(SRC_PATH)
            .in_folder("*providers*")
            .should_not()
            .depend_on_files()
            .in_path(forbidden)
        )
        assert_passes(rule)
    for library in ("sqlalchemy*", "alembic*"):
        library_rule = (
            project_files(SRC_PATH)
            .in_folder("*providers*")
            .should_not()
            .depend_on_external_modules()
            .matching(library)
        )
        assert_passes(library_rule)


def test_nothing_depends_on_a_provider_from_the_foundation_or_auth() -> None:
    """Only the web and core layers wire providers in; the foundation never does."""
    for lower in ("*db.py", "*config.py", "*auth*"):
        rule = (
            project_files(SRC_PATH)
            .in_path(lower)
            .should_not()
            .depend_on_files()
            .in_folder("*providers*")
        )
        assert_passes(rule)


BANNED_HTTP_CLIENTS = {"requests", "httpx"}


def _top_level_imports(source: Path) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    return imported


def test_the_only_http_client_is_httpx2() -> None:
    """`httpx2` is the project's client, as in Menagerist. Not `requests`, not `httpx`.

    This reads each file's imports instead of using an archunit pattern, because that
    matches by dotted segment and would flag `starlette.requests`.
    """
    offenders = {
        str(path.relative_to(SRC_PATH)): sorted(found)
        for path in Path(SRC_PATH).rglob("*.py")
        if (found := _top_level_imports(path) & BANNED_HTTP_CLIENTS)
    }

    assert offenders == {}
