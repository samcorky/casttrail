from pathlib import Path

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
