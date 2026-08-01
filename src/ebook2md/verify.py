from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from bs4 import BeautifulSoup

from .formatting import is_formatted_idempotently
from .lists import BULLETS, validate_list_node
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
    all_markdown = ""
    manifest_paths = {asset.get("path", "") for asset in manifest.get("assets", [])}
    manifest_ids = {asset.get("id", "") for asset in manifest.get("assets", [])}
    for markdown_path in markdown_paths:
        if not markdown_path.exists():
            errors.append(f"missing Markdown file: {markdown_path.relative_to(root)}")
            continue
        markdown = markdown_path.read_text(encoding="utf-8")
        all_markdown += "\n" + markdown
        if not is_formatted_idempotently(markdown):
            errors.append(f"formatter is not idempotent: {markdown_path.relative_to(root)}")
        if re.search(r"<\|/?(?:ref|det)\|>", markdown):
            errors.append(f"raw grounding token: {markdown_path.relative_to(root)}")
        if re.search(r"!\[[^\]]*\]\(\s*data:", markdown, re.I):
            errors.append(f"base64 asset: {markdown_path.relative_to(root)}")
        if re.search(r"\]\((?:file://|/Users/|/[A-Za-z0-9_.-]+/)", markdown):
            errors.append(f"absolute path: {markdown_path.relative_to(root)}")
        _verify_tables(markdown, markdown_path, root, errors)
        _verify_rendered_lists(markdown, markdown_path, root, errors)
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
        for target in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", markdown):
            clean = unquote(target.partition("#")[0])
            resolved = (markdown_path.parent / clean).resolve()
            try:
                manifest_path = str(resolved.relative_to(root))
            except ValueError:
                continue
            if manifest_path not in manifest_paths:
                errors.append(
                    f"displayed asset lacks manifest entry: {markdown_path.relative_to(root)} -> {target}"
                )
    seen_paths = set()
    for asset in manifest.get("assets", []):
        path = asset.get("path", "")
        if not path or not (root / path).exists():
            errors.append(f"missing asset: {path or asset.get('id', '<unknown>')}")
        seen_paths.add(path)
        original = asset.get("original_path")
        if original and not (root / original).exists():
            warnings.append(f"missing original asset: {original}")
        evidence = asset.get("evidence_path")
        if evidence and not (root / evidence).exists():
            warnings.append(f"missing evidence asset: {evidence}")
    pages = document.get("pages", [])
    page_numbers = [page.get("number") for page in pages]
    if page_numbers != sorted(page_numbers) or len(page_numbers) != len(set(page_numbers)):
        errors.append("pages are reordered or duplicated")
    if len(page_numbers) != metadata.get("requested_page_count", len(page_numbers)):
        errors.append("page count differs from requested page count")
    comments = [int(value) for value in re.findall(r"<!-- page: (\d+) -->", all_markdown)]
    malformed_comments = re.findall(r"<!--\s*page\s*:[^>]*-->", all_markdown, re.I)
    if len(malformed_comments) != len(comments):
        errors.append("malformed page comment")
    if comments != page_numbers:
        errors.append("page comments are missing, duplicated, or reordered")
    observation_ids: set[str] = set()
    for page in pages:
        for block in page.get("blocks", []):
            if block.get("kind") == "list":
                node = block.get("metadata", {}).get("list")
                if not isinstance(node, dict):
                    errors.append(f"list block lacks normalized node on page {page.get('number')}")
                else:
                    errors.extend(
                        f"invalid list on page {page.get('number')}: {error}"
                        for error in validate_list_node(node)
                    )
                    _verify_preserved_list_labels(block, node, page.get("number"), errors)
            if block.get("kind") in {"figure", "embedded_figure"}:
                asset_id = block.get("asset_id")
                if not asset_id or asset_id not in manifest_ids:
                    warnings.append(
                        f"figure block lacks manifest asset on page {page.get('number')}"
                    )
            if (
                block.get("kind") == "table"
                and "<table" in block.get("markdown", "").casefold()
                and not block.get("metadata", {}).get("html_fallback_reason")
            ):
                errors.append(f"HTML table lacks fallback reason on page {page.get('number')}")
        visual = page.get("visual", {})
        observations = [
            visual.get("multi_page", {}),
            *visual.get("candidates", visual.get("gundam", [])),
        ]
        for observation in observations:
            if not observation:
                continue
            observation_ids.add(observation.get("id", ""))
            raw_path = observation.get("raw_path", "")
            if not raw_path or not (root / raw_path).exists():
                errors.append(f"missing raw observation: {observation.get('id', '<unknown>')}")
        for recovery in page.get("recovery", []):
            for key in ("primary_observation", "recovery_observation"):
                reference = recovery.get(key)
                if not reference or reference not in observation_ids | {
                    item.get("id", "") for item in observations
                }:
                    errors.append(f"recovery provenance references unknown observation: {reference}")
    if metadata.get("formatting", {}).get("idempotent") is not True:
        errors.append("metadata reports non-idempotent formatting")
    if metadata.get("formatting", {}).get("lint_errors"):
        warnings.append("metadata reports Markdown lint failures")
    if metadata.get("resume_stable") is False:
        errors.append("resume changed Markdown filenames or content")
    for failed in metadata.get("failed_pages", []):
        warnings.append(f"page {failed.get('page')} failed: {failed.get('error')}")
    warnings.extend(metadata.get("warnings", []))
    return Verification(not errors, errors, sorted(set(warnings)), len(markdown_paths), len(manifest.get("assets", [])))


def _verify_tables(markdown: str, markdown_path: Path, root: Path, errors: list[str]) -> None:
    for table in re.findall(r"<table\b.*?</table>", markdown, re.I | re.S):
        parsed = BeautifulSoup(table, "html.parser").find("table")
        if parsed is None or not parsed.find("tr"):
            errors.append(f"malformed HTML table: {markdown_path.relative_to(root)}")
    lines = markdown.splitlines()
    for index, line in enumerate(lines):
        if not re.match(r"^\|(?:\s*:?-+:?\s*\|)+$", line):
            continue
        if index == 0 or index + 1 >= len(lines):
            errors.append(f"malformed GFM table: {markdown_path.relative_to(root)}:{index + 1}")
            continue
        width = _pipe_width(lines[index - 1])
        row = index + 1
        while row < len(lines) and lines[row].startswith("|"):
            if _pipe_width(lines[row]) != width:
                errors.append(f"inconsistent GFM table: {markdown_path.relative_to(root)}:{row + 1}")
                break
            row += 1


def _verify_rendered_lists(markdown: str, markdown_path: Path, root: Path, errors: list[str]) -> None:
    visual_bullet = re.compile(rf"^[ \t]*[{re.escape(BULLETS)}]\s+", re.MULTILINE)
    if visual_bullet.search(markdown):
        errors.append(f"raw visual list bullet: {markdown_path.relative_to(root)}")
    marker = re.compile(r"^(?P<indent> +)(?:[-+*]|\d+\.)\s+", re.MULTILINE)
    for match in marker.finditer(markdown):
        width = len(match.group("indent"))
        if width < 2:
            line = markdown.count("\n", 0, match.start()) + 1
            errors.append(f"invalid nested list indentation: {markdown_path.relative_to(root)}:{line}")


def _verify_preserved_list_labels(
    block: dict, node: dict, page: int | None, errors: list[str]
) -> None:
    markdown = block.get("markdown", "")
    for item in node.get("items", []):
        if node.get("marker_style") in {"alpha", "roman"}:
            marker = item.get("source_marker", "")
            if marker and f"**{marker}**" not in markdown:
                errors.append(f"list label {marker!r} was not rendered on page {page}")
        for child in item.get("children", []):
            _verify_preserved_list_labels({"markdown": markdown}, child, page, errors)


def _pipe_width(line: str) -> int:
    return len(re.findall(r"(?<!\\)\|", line)) - 1
