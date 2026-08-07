from __future__ import annotations

import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import fitz
from PIL import Image

from .assets import AssetStore
from .model import EmbeddedEvidence, Link, SourceDocument, SourcePage
from .util import natural_key

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
            epub_mimetype = b""
            if "mimetype" in names:
                with archive.open("mimetype") as stream:
                    epub_mimetype = stream.read(64).strip()
            if "META-INF/container.xml" in names or (
                "mimetype" in names
                and epub_mimetype == b"application/epub+zip"
            ):
                raise ValueError(f"unsupported input format: {path}")
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
) -> SourceDocument:
    kind = detect_kind(source)
    if kind == "pdf":
        return _open_pdf(source, work, assets, dpi=dpi)
    if kind == "djvu":
        return _open_djvu(source, work, dpi=dpi)
    if kind == "cbz":
        return _open_cbz(source, work)
    return _open_images(source, work)


def _open_pdf(source: Path, work: Path, assets: AssetStore, *, dpi: int) -> SourceDocument:
    pages_dir = work / "rendered"
    pages_dir.mkdir(parents=True, exist_ok=True)
    result = SourceDocument(path=source, kind="pdf")
    with fitz.open(source) as document:
        selected = range(1, document.page_count + 1)
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
                rects = [rect for rect in page.get_image_rects(xref) if not _is_page_backing_image(rect, page.rect)]
                if not rects:
                    continue
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
                for rect in rects:
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


def _is_page_backing_image(rect, page_rect) -> bool:
    """A scan covering the page is OCR input, not a document figure."""
    if page_rect.width <= 0 or page_rect.height <= 0:
        return False
    width_ratio = rect.width / page_rect.width
    height_ratio = rect.height / page_rect.height
    return width_ratio >= 0.90 and height_ratio >= 0.90


def _open_djvu(source: Path, work: Path, *, dpi: int) -> SourceDocument:
    for command in ("ddjvu", "djvused"):
        if not shutil.which(command):
            raise RuntimeError(f"{command} is required for DjVu input")
    count_output = subprocess.run(
        ["djvused", str(source), "-e", "n"], check=True, capture_output=True, text=True
    ).stdout.strip()
    total = int(count_output)
    selected = range(1, total + 1)
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


def _open_images(source: Path, work: Path) -> SourceDocument:
    paths = sorted((path for path in source.iterdir() if path.suffix.lower() in IMAGE_EXTENSIONS), key=natural_key) if source.is_dir() else [source]
    frames: list[tuple[Path, int | None]] = []
    for path in paths:
        with Image.open(path) as image:
            if getattr(image, "n_frames", 1) > 1:
                for frame_index in range(image.n_frames):
                    frames.append((path, frame_index))
            else:
                frames.append((path, None))
    selected = range(1, len(frames) + 1)
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


def _open_cbz(source: Path, work: Path) -> SourceDocument:
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
    return _open_images(extracted, work)
