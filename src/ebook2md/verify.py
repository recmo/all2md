from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .markdown import local_links, markdown_anchors


@dataclass
class Verification:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    markdown_files: int = 0
    assets: int = 0


def verify_bundle(root: Path) -> Verification:
    root = root.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    for required in ("book.md", "document.json", "metadata.json", "conversion.log", "assets/manifest.json"):
        if not (root / required).exists():
            errors.append(f"missing {required}")
    if errors:
        return Verification(False, errors)
    metadata = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    document = json.loads((root / "document.json").read_text(encoding="utf-8"))
    manifest = json.loads((root / "assets/manifest.json").read_text(encoding="utf-8"))
    markdown_paths = [root / path for path in metadata.get("markdown_files", ["book.md"])]
    for markdown_path in markdown_paths:
        if not markdown_path.exists():
            errors.append(f"missing Markdown file: {markdown_path.relative_to(root)}")
            continue
        markdown = markdown_path.read_text(encoding="utf-8")
        for target in local_links(markdown):
            clean_target, _, anchor = target.partition("#")
            target_path = markdown_path if not clean_target else markdown_path.parent / clean_target
            if clean_target and not (markdown_path.parent / clean_target).resolve().is_relative_to(root):
                errors.append(f"link escapes bundle: {markdown_path.relative_to(root)} -> {target}")
            elif clean_target and not target_path.exists():
                errors.append(f"broken link: {markdown_path.relative_to(root)} -> {target}")
            elif anchor and target_path.exists():
                target_markdown = target_path.read_text(encoding="utf-8")
                anchors = markdown_anchors(target_markdown)
                if anchor not in anchors:
                    errors.append(f"broken anchor: {markdown_path.relative_to(root)} -> {target}")
    seen_paths = set()
    for asset in manifest.get("assets", []):
        path = asset.get("path", "")
        if not path or not (root / path).exists():
            errors.append(f"missing asset: {path or asset.get('id', '<unknown>')}")
        seen_paths.add(path)
        original = asset.get("original_path")
        if original and not (root / original).exists():
            errors.append(f"missing original asset: {original}")
    pages = document.get("pages", [])
    page_numbers = [page.get("number") for page in pages]
    if page_numbers != sorted(page_numbers) or len(page_numbers) != len(set(page_numbers)):
        errors.append("pages are reordered or duplicated")
    for failed in metadata.get("failed_pages", []):
        warnings.append(f"page {failed.get('page')} failed: {failed.get('error')}")
    warnings.extend(metadata.get("warnings", []))
    return Verification(not errors, errors, sorted(set(warnings)), len(markdown_paths), len(manifest.get("assets", [])))
