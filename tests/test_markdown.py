from pathlib import Path

from ebook2md.markdown import local_links, write_markdown
from ebook2md.model import Chapter, Comparison, EmbeddedEvidence, PageResult


def page(number: int) -> PageResult:
    return PageResult(
        number=number,
        image=f"page-{number}.png",
        visual_markdown="Text\n\n![Figure](assets/figures/fig-0001.png)",
        blocks=[],
        embedded=EmbeddedEvidence(),
        comparison=Comparison(),
    )


def test_chapter_links_are_relative(tmp_path: Path):
    (tmp_path / "assets/figures").mkdir(parents=True)
    (tmp_path / "assets/figures/fig-0001.png").write_bytes(b"png")
    files = write_markdown(
        tmp_path,
        [page(1), page(2)],
        [Chapter("One", 1, 1, "one"), Chapter("Two", 2, 2, "two")],
        split=True,
        title="Book",
    )
    assert files == ["book.md", "chapters/000-one.md", "chapters/001-two.md"]
    chapter = (tmp_path / "chapters/000-one.md").read_text()
    assert "../assets/figures/fig-0001.png" in chapter
    assert local_links(chapter) == ["../assets/figures/fig-0001.png"]

