import os
from pathlib import Path


FORBIDDEN_IDENTIFIERS = (
    "a" + "mie",
    "brain" + "-executive",
    "broni" + "woja",
    "knowledge" + "-ingress",
    "pk" + "-proxy",
    "world" + "-foundation",
)


def test_doc2md_contains_no_environment_specific_identifiers() -> None:
    project = Path(__file__).parents[1]
    configured_root = os.environ.get("DOC2MD_REPOSITORY_ROOT")
    repository = Path(configured_root) if configured_root else next(
        (
            parent
            for parent in project.parents
            if (parent / "flake.nix").is_file() and (parent / "tools/doc2md").is_dir()
        ),
        project,
    )
    files = [
        path
        for path in repository.rglob("*")
        if path.is_file()
        and path != Path(__file__)
        and ".git" not in path.parts
        and path.suffix in {".json", ".md", ".py", ".toml"}
    ]
    contents = "\n".join(path.read_text(errors="ignore").lower() for path in files)

    for identifier in FORBIDDEN_IDENTIFIERS:
        assert identifier not in contents
