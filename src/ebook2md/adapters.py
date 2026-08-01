from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from urllib.parse import unquote

import fitz
from bs4 import BeautifulSoup
from PIL import Image, ImageSequence

from .assets import AssetStore
from .model import EmbeddedEvidence, Link, SourceDocument, SourcePage
from .util import natural_key, parse_pages

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}


def _link_target(link: dict[str, object]) -> str:
    uri = link.get("uri")
    if isinstance(uri, str) and uri:
        return uri
    page = link.get("page", -1)
    try:
        page_number = int(page)
    except (TypeError, ValueError):
        return ""
    return f"#page-{page_number + 1}" if page_number >= 0 else ""


def detect_kind(path: Path) -> str:
    if path.is_dir():
        return "images"
    header = path.read_bytes()[:16]
    if header.startswith(b"%PDF"):
        return "pdf"
    if header.startswith((b"AT&TFORM", b"FORM")) and b"DJV" in path.read_bytes()[:32]:
        return "djvu"
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "META-INF/container.xml" in names or "mimetype" in names:
                return "epub"
        return "cbz"
    if path.suffix.lower() in IMAGE_EXTENSIONS:
        return "image"
    raise ValueError(f"unsupported input format: {path}")


def open_document(
    source: Path,
    work: Path,
    assets: AssetStore,
    *,
    dpi: int,
    page_spec: str | None,
) -> SourceDocument:
    kind = detect_kind(source)
    if kind == "pdf":
        return _open_pdf(source, work, assets, dpi=dpi, page_spec=page_spec)
    if kind == "djvu":
        return _open_djvu(source, work, dpi=dpi, page_spec=page_spec)
    if kind == "epub":
        return _open_epub(source, work, assets, dpi=dpi, page_spec=page_spec)
    if kind == "cbz":
        return _open_cbz(source, work, page_spec=page_spec)
    return _open_images(source, work, page_spec=page_spec)


def _open_pdf(source: Path, work: Path, assets: AssetStore, *, dpi: int, page_spec: str | None) -> SourceDocument:
    pages_dir = work / "rendered"
    pages_dir.mkdir(parents=True, exist_ok=True)
    result = SourceDocument(path=source, kind="pdf")
    with fitz.open(source) as document:
        selected = parse_pages(page_spec, document.page_count)
        result.metadata = dict(document.metadata or {})
        result.outline = [
            {"level": level, "title": title, "page": page}
            for level, title, page, *_ in document.get_toc(simple=False)
            if page > 0
        ]
        matrix = fitz.Matrix(dpi / 72, dpi / 72)
        extracted_xrefs: dict[int, str] = {}
        for page_number in selected:
            page = document[page_number - 1]
            image_path = pages_dir / f"page-{page_number:04d}.png"
            page.get_pixmap(matrix=matrix, alpha=False).save(image_path)
            text_dict = page.get_text("dict", sort=True)
            embedded_blocks = []
            for block in text_dict.get("blocks", []):
                if block.get("type") != 0:
                    continue
                lines = []
                for line in block.get("lines", []):
                    lines.append("".join(span.get("text", "") for span in line.get("spans", [])))
                text = "\n".join(lines).strip()
                if text:
                    bbox = block.get("bbox", ())
                    embedded_blocks.append({
                        "text": text,
                        "bbox": [
                            bbox[0] * 1000 / page.rect.width,
                            bbox[1] * 1000 / page.rect.height,
                            bbox[2] * 1000 / page.rect.width,
                            bbox[3] * 1000 / page.rect.height,
                        ] if len(bbox) == 4 else [],
                        "space": "normalized_1000",
                    })
            links = []
            for link in page.get_links():
                target = _link_target(link)
                if target:
                    rect = link.get("from")
                    label = page.get_textbox(rect).strip() if rect else ""
                    bbox = (
                        rect.x0 * 1000 / page.rect.width,
                        rect.y0 * 1000 / page.rect.height,
                        rect.x1 * 1000 / page.rect.width,
                        rect.y1 * 1000 / page.rect.height,
                    ) if rect else None
                    links.append(Link(text=label, target=target, bbox=bbox, external=bool(link.get("uri"))))
            source_assets: list[dict[str, object]] = []
            for image in page.get_images(full=True):
                xref = image[0]
                if xref in extracted_xrefs:
                    asset_id = extracted_xrefs[xref]
                else:
                    extracted = document.extract_image(xref)
                    asset = assets.add_bytes(
                        extracted["image"],
                        extension=extracted.get("ext", "bin"),
                        page=page_number,
                        bbox=None,
                        method="pdf_embedded_object",
                        source_object=f"xref:{xref}",
                        alt_text="Embedded figure",
                    )
                    asset_id = asset.id
                    extracted_xrefs[xref] = asset_id
                for rect in page.get_image_rects(xref):
                    bbox = [
                        rect.x0 * 1000 / page.rect.width,
                        rect.y0 * 1000 / page.rect.height,
                        rect.x1 * 1000 / page.rect.width,
                        rect.y1 * 1000 / page.rect.height,
                    ]
                    assets.add_placement(
                        asset_id,
                        page=page_number,
                        bbox=bbox,
                        method="pdf_embedded_placement",
                        source_object=f"xref:{xref}",
                    )
                    source_assets.append({
                        "asset_id": asset_id,
                        "bbox": bbox,
                        "space": "normalized_1000",
                    })
            embedded = EmbeddedEvidence(
                text="\n\n".join(block["text"] for block in embedded_blocks),
                blocks=embedded_blocks,
                links=links,
                extractor="pymupdf",
            )
            result.pages.append(SourcePage(page_number, image_path, embedded, source_assets))
    return result


def _open_djvu(source: Path, work: Path, *, dpi: int, page_spec: str | None) -> SourceDocument:
    for command in ("ddjvu", "djvused"):
        if not shutil.which(command):
            raise RuntimeError(f"{command} is required for DjVu input")
    count_output = subprocess.run(
        ["djvused", str(source), "-e", "n"], check=True, capture_output=True, text=True
    ).stdout.strip()
    total = int(count_output)
    selected = parse_pages(page_spec, total)
    pages_dir = work / "rendered"
    pages_dir.mkdir(parents=True, exist_ok=True)
    result = SourceDocument(path=source, kind="djvu")
    outline_output = subprocess.run(
        ["djvused", str(source), "-e", "print-outline"],
        check=False,
        capture_output=True,
        text=True,
    ).stdout
    result.outline = [
        {
            "level": 1,
            "title": title.replace(r'\"', '"').replace(r"\\", "\\"),
            "page": int(page),
        }
        for title, page in re.findall(r'\("((?:\\.|[^"\\])*)"\s+"#(\d+)"', outline_output)
    ]
    for page_number in selected:
        image_path = pages_dir / f"page-{page_number:04d}.png"
        subprocess.run(
            ["ddjvu", "-format=png", f"-page={page_number}", f"-dpi={dpi}", str(source), str(image_path)],
            check=True,
        )
        text = subprocess.run(
            ["djvused", str(source), "-e", f"select {page_number}; print-pure-txt"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        structured = subprocess.run(
            ["djvused", str(source), "-e", f"select {page_number}; print-txt"],
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        annotations = subprocess.run(
            ["djvused", str(source), "-e", f"select {page_number}; print-ant"],
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        result.pages.append(
            SourcePage(
                page_number,
                image_path,
                EmbeddedEvidence(
                    text=text,
                    blocks=[
                        {
                            "format": "djvused-s_expression",
                            "structured_text": structured,
                            "annotations": annotations,
                        }
                    ],
                    extractor="djvused:print-pure-txt+print-txt+print-ant",
                ),
            )
        )
    return result


def _open_images(source: Path, work: Path, *, page_spec: str | None) -> SourceDocument:
    paths = sorted((path for path in source.iterdir() if path.suffix.lower() in IMAGE_EXTENSIONS), key=natural_key) if source.is_dir() else [source]
    frames: list[tuple[Path, int | None]] = []
    for path in paths:
        with Image.open(path) as image:
            if getattr(image, "n_frames", 1) > 1:
                for frame_index in range(image.n_frames):
                    frames.append((path, frame_index))
            else:
                frames.append((path, None))
    selected = parse_pages(page_spec, len(frames))
    pages_dir = work / "rendered"
    pages_dir.mkdir(parents=True, exist_ok=True)
    result = SourceDocument(path=source, kind="images")
    for page_number in selected:
        path, frame_index = frames[page_number - 1]
        destination = pages_dir / f"page-{page_number:04d}.png"
        with Image.open(path) as image:
            if frame_index is not None:
                image.seek(frame_index)
            image.convert("RGB").save(destination, format="PNG")
        result.pages.append(SourcePage(page_number, destination))
    return result


def _open_cbz(source: Path, work: Path, *, page_spec: str | None) -> SourceDocument:
    extracted = work / "cbz"
    extracted.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as archive:
        members = sorted(
            (name for name in archive.namelist() if Path(name).suffix.lower() in IMAGE_EXTENSIONS),
            key=lambda name: natural_key(Path(name)),
        )
        for index, member in enumerate(members, 1):
            destination = extracted / f"{index:06d}{Path(member).suffix.lower()}"
            destination.write_bytes(archive.read(member))
    return _open_images(extracted, work, page_spec=page_spec)


def _open_epub(
    source: Path,
    work: Path,
    assets: AssetStore,
    *,
    dpi: int,
    page_spec: str | None,
) -> SourceDocument:
    result = SourceDocument(path=source, kind="epub")
    with zipfile.ZipFile(source) as archive:
        container = BeautifulSoup(archive.read("META-INF/container.xml"), "xml")
        rootfile = container.find("rootfile")
        if not rootfile or not rootfile.get("full-path"):
            raise ValueError("EPUB has no package document")
        package_path = Path(unquote(rootfile["full-path"]))
        package = BeautifulSoup(archive.read(str(package_path)), "xml")
        package_title = package.find("dc:title") or package.find("title")
        if package_title and package_title.get_text(" ", strip=True):
            result.metadata["title"] = package_title.get_text(" ", strip=True)
        rendition = package.find("meta", attrs={"property": "rendition:layout"})
        if rendition is None:
            rendition = package.find("meta", attrs={"name": "rendition:layout"})
        layout = (
            rendition.get("content", "") or rendition.get_text(" ", strip=True)
            if rendition is not None
            else ""
        )
        manifest = {item.get("id"): item for item in package.find_all("item")}
        spine = [itemref.get("idref") for itemref in package.find_all("itemref")]
        asset_by_member: dict[str, str] = {}
        for identifier, item in manifest.items():
            media_type = item.get("media-type", "")
            if not (media_type.startswith("image/") or media_type == "image/svg+xml"):
                continue
            member = str((package_path.parent / unquote(item.get("href", ""))).as_posix())
            if member not in archive.namelist():
                continue
            asset = assets.add_bytes(
                archive.read(member),
                extension=Path(member).suffix,
                page=0,
                bbox=None,
                method="epub_embedded_object",
                source_object=identifier,
                alt_text=Path(member).stem.replace("-", " "),
            )
            asset_by_member[member] = asset.path
        for identifier in spine:
            item = manifest.get(identifier)
            if not item:
                continue
            member = str((package_path.parent / unquote(item.get("href", ""))).as_posix())
            if member not in archive.namelist():
                continue
            soup = BeautifulSoup(archive.read(member), "html.parser")
            title_node = soup.find(["h1", "h2", "title"])
            title = title_node.get_text(" ", strip=True) if title_node else f"Chapter {len(result.semantic_chapters) + 1}"
            for image in soup.find_all("img"):
                source_member = str((Path(member).parent / unquote(image.get("src", ""))).as_posix())
                if source_member in asset_by_member:
                    image.replace_with(f"\n\n![{image.get('alt') or 'Figure'}]({asset_by_member[source_member]})\n\n")
            for link in soup.find_all("a"):
                label = link.get_text(" ", strip=True)
                target = link.get("href")
                if target and label:
                    link.replace_with(f"[{label}]({target})")
            markdown_lines: list[str] = []
            for node in soup.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
                text = node.get_text(" ", strip=True)
                if not text:
                    continue
                if node.name.startswith("h"):
                    markdown_lines.append(f"{'#' * int(node.name[1])} {text}")
                elif node.name == "li":
                    markdown_lines.append(f"- {text}")
                else:
                    markdown_lines.append(text)
            result.semantic_chapters.append({"title": title, "markdown": "\n\n".join(markdown_lines), "source": member})
    if layout.casefold() == "pre-paginated":
        return _open_fixed_epub(source, work, result, dpi=dpi, page_spec=page_spec)
    return result


def _open_fixed_epub(
    source: Path,
    work: Path,
    semantic: SourceDocument,
    *,
    dpi: int,
    page_spec: str | None,
) -> SourceDocument:
    pages_dir = work / "rendered"
    pages_dir.mkdir(parents=True, exist_ok=True)
    result = SourceDocument(
        path=source,
        kind="epub-fixed",
        semantic_chapters=semantic.semantic_chapters,
        metadata=semantic.metadata,
    )
    with fitz.open(source) as document:
        result.outline = [
            {"level": level, "title": title, "page": page}
            for level, title, page, *_ in document.get_toc(simple=False)
            if page > 0
        ]
        selected = parse_pages(page_spec, document.page_count)
        matrix = fitz.Matrix(dpi / 72, dpi / 72)
        for page_number in selected:
            page = document[page_number - 1]
            image_path = pages_dir / f"page-{page_number:04d}.png"
            page.get_pixmap(matrix=matrix, alpha=False).save(image_path)
            blocks = []
            for block in page.get_text("blocks", sort=True):
                if len(block) >= 5 and str(block[4]).strip():
                    blocks.append({
                        "text": str(block[4]).strip(),
                        "bbox": [
                            block[0] * 1000 / page.rect.width,
                            block[1] * 1000 / page.rect.height,
                            block[2] * 1000 / page.rect.width,
                            block[3] * 1000 / page.rect.height,
                        ],
                        "space": "normalized_1000",
                    })
            links = []
            for link in page.get_links():
                target = _link_target(link)
                rect = link.get("from")
                if target:
                    links.append(Link(
                        text=page.get_textbox(rect).strip() if rect else "",
                        target=target,
                        bbox=(
                            rect.x0 * 1000 / page.rect.width,
                            rect.y0 * 1000 / page.rect.height,
                            rect.x1 * 1000 / page.rect.width,
                            rect.y1 * 1000 / page.rect.height,
                        ) if rect else None,
                        external=bool(link.get("uri")),
                    ))
            result.pages.append(SourcePage(
                page_number,
                image_path,
                EmbeddedEvidence(
                    text="\n\n".join(block["text"] for block in blocks),
                    blocks=blocks,
                    links=links,
                    extractor="pymupdf-epub-fixed",
                ),
            ))
    return result
