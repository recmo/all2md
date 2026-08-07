from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


Box = tuple[float, float, float, float]


@dataclass
class Link:
    text: str
    target: str
    bbox: Box | None = None
    external: bool = True


@dataclass
class EmbeddedEvidence:
    text: str = ""
    blocks: list[dict[str, Any]] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    extractor: str | None = None


@dataclass
class Block:
    kind: str
    markdown: str
    bbox: Box | None = None
    confidence: float | None = None
    asset_id: str | None = None
    source_pages: list[int] = field(default_factory=list)
    provenance: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class OcrObservation:
    id: str
    mode: str
    raw: str
    source_pages: list[int]
    generation: dict[str, Any] = field(default_factory=dict)
    blocks: list[Block] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class Comparison:
    character_similarity: float | None = None
    token_coverage: float | None = None
    length_ratio: float | None = None
    reading_order_differs: bool = False
    math_symbol_differs: bool = False
    warnings: list[str] = field(default_factory=list)
    disagreements: list[dict[str, str]] = field(default_factory=list)


@dataclass
class PageResult:
    number: int
    image: str
    visual_markdown: str
    blocks: list[Block]
    embedded: EmbeddedEvidence
    comparison: Comparison
    warnings: list[str] = field(default_factory=list)
    generation: dict[str, Any] = field(default_factory=dict)
    source_assets: list[dict[str, Any]] = field(default_factory=list)
    raw_ocr: str = ""
    visual: dict[str, Any] = field(default_factory=dict)
    recovery: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Chapter:
    title: str
    start_page: int
    end_page: int
    slug: str
    confidence: float = 1.0
    evidence: str = "manual"


@dataclass
class SourcePage:
    number: int
    image_path: Path
    embedded: EmbeddedEvidence = field(default_factory=EmbeddedEvidence)
    source_assets: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SourceDocument:
    path: Path
    kind: str
    pages: list[SourcePage] = field(default_factory=list)
    outline: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
