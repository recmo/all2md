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


def _normalized_bbox(bbox, page_rect) -> list[float]:
    if not bbox or len(bbox) != 4:
        return []
    return [
        float(bbox[0]) * 1000 / page_rect.width,
        float(bbox[1]) * 1000 / page_rect.height,
        float(bbox[2]) * 1000 / page_rect.width,
        float(bbox[3]) * 1000 / page_rect.height,
    ]


def _raw_text_blocks(page) -> list[dict[str, object]]:
    """Retain PDF character geometry and font metadata as optional evidence."""
    output: list[dict[str, object]] = []
    font_ids: dict[str, set[int]] = {}
    for font in page.get_fonts():
        name = re.sub(r"^[A-Z]{6}\+", "", font[3])
        font_ids.setdefault(name, set()).add(font[0])
    raw = page.get_text("rawdict", sort=True)
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        lines: list[dict[str, object]] = []
        block_text: list[str] = []
        for line in block.get("lines", []):
            spans: list[dict[str, object]] = []
            line_text: list[str] = []
            for span in line.get("spans", []):
                characters = []
                for character in span.get("chars", []):
                    value = character.get("c", "")
                    if not value:
                        continue
                    character_bbox = _normalized_bbox(character.get("bbox"), page.rect)
                    characters.append({
                        "text": value,
                        "bbox": character_bbox,
                        "origin": [
                            float(character["origin"][0]) * 1000 / page.rect.width,
                            float(character["origin"][1]) * 1000 / page.rect.height,
                        ] if character.get("origin") else None,
                        "space": "normalized_1000",
                    })
                    line_text.append(value)
                span_text = "".join(item["text"] for item in characters)
                if not span_text:
                    continue
                spans.append({
                    "text": span_text,
                    "bbox": _normalized_bbox(span.get("bbox"), page.rect),
                    "space": "normalized_1000",
                    "font": span.get("font", ""),
                    "font_xrefs": sorted(font_ids.get(span.get("font", ""), set())),
                    "size": span.get("size"),
                    "em": [
                        float(span.get("size", 0)) * 1000 / page.rect.width,
                        float(span.get("size", 0)) * 1000 / page.rect.height,
                    ],
                    "flags": span.get("flags"),
                    "chars": characters,
                })
            text = "".join(line_text)
            if text:
                block_text.append(text)
                lines.append({
                    "text": text,
                    "direction": list(line.get("dir", (1, 0))),
                    "bbox": _normalized_bbox(line.get("bbox"), page.rect),
                    "space": "normalized_1000",
                    "spans": spans,
                })
        text = "\n".join(block_text).strip()
        if text:
            output.append({
                "text": text,
                "bbox": _normalized_bbox(block.get("bbox"), page.rect),
                "space": "normalized_1000",
                "lines": lines,
            })
    return output


def _characters_in_box(
    blocks: list[dict[str, object]],
    bbox: tuple[float, float, float, float],
) -> str:
    """Read only glyphs whose centers lie inside an annotation rectangle."""
    selected: list[str] = []
    left, top, right, bottom = bbox
    tolerance = 1.5
    for block in blocks:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for character in span.get("chars", []):
                    box = character.get("bbox", [])
                    if len(box) != 4:
                        continue
                    x = (box[0] + box[2]) / 2
                    y = (box[1] + box[3]) / 2
                    if left - tolerance <= x <= right + tolerance and top - tolerance <= y <= bottom + tolerance:
                        selected.append(character.get("text", ""))
    return "".join(selected).strip()


def _coalesce_links(links: list[Link]) -> list[Link]:
    """Join adjacent annotation rectangles that encode one wrapped URI label."""
    output: list[Link] = []
    for link in links:
        if not link.text:
            continue
        previous = output[-1] if output else None
        if (
            previous
            and previous.target == link.target
            and previous.external == link.external
            and previous.bbox
            and link.bbox
            and -max(
                previous.bbox[3] - previous.bbox[1],
                link.bbox[3] - link.bbox[1],
            ) - 3
            <= link.bbox[1] - previous.bbox[3]
            <= 35
        ):
            previous.text = f"{previous.text}{link.text}"
            previous.bbox = (
                min(previous.bbox[0], link.bbox[0]),
                min(previous.bbox[1], link.bbox[1]),
                max(previous.bbox[2], link.bbox[2]),
                max(previous.bbox[3], link.bbox[3]),
            )
            continue
        output.append(link)
    return output


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
    ignore_embedded_text: bool = False,
) -> SourceDocument:
    kind = detect_kind(source)
    if kind == "pdf":
        return _open_pdf(
            source,
            work,
            assets,
            dpi=dpi,
            ignore_embedded_text=ignore_embedded_text,
        )
    if kind == "djvu":
        return _open_djvu(
            source,
            work,
            dpi=dpi,
            ignore_embedded_text=ignore_embedded_text,
        )
    if kind == "cbz":
        return _open_cbz(source, work)
    return _open_images(source, work)


def _open_pdf(
    source: Path,
    work: Path,
    assets: AssetStore,
    *,
    dpi: int,
    ignore_embedded_text: bool,
) -> SourceDocument:
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
            embedded_blocks = [] if ignore_embedded_text else _raw_text_blocks(page)
            links = []
            if not ignore_embedded_text:
                for link in page.get_links():
                    target = _link_target(link)
                    if target:
                        rect = link.get("from")
                        normalized = _normalized_bbox(rect, page.rect) if rect else []
                        bbox = tuple(normalized) if normalized else None
                        label = _characters_in_box(embedded_blocks, bbox) if bbox else ""
                        links.append(Link(text=label, target=target, bbox=bbox, external=bool(link.get("uri"))))
                links = _coalesce_links(links)
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
                extractor="ignored" if ignore_embedded_text else "pymupdf",
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


def _open_djvu(
    source: Path,
    work: Path,
    *,
    dpi: int,
    ignore_embedded_text: bool,
) -> SourceDocument:
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
        text = ""
        structured = ""
        annotations = ""
        if not ignore_embedded_text:
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
                    ] if not ignore_embedded_text else [],
                    extractor=(
                        "djvused:print-pure-txt+print-txt+print-ant"
                        if not ignore_embedded_text
                        else "ignored"
                    ),
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
