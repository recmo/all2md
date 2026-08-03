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
    files = [
        path
        for path in project.rglob("*")
        if path.is_file()
        and path != Path(__file__)
        and path.suffix in {".json", ".md", ".py", ".toml"}
    ]
    contents = "\n".join(path.read_text(errors="ignore").lower() for path in files)

    for identifier in FORBIDDEN_IDENTIFIERS:
        assert identifier not in contents
