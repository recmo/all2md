"""Bounded, soft OCR decoding guidance. No model weights or source text are edited.

Source occurrences are consumed only by emitted words, never by speculative
candidate scoring. Geometry selects local evidence; unsupported regions abstain.
The structural detector abstracts counters for scoring only, not transcription.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
import unicodedata

from .embedded import assess_embedded, bbox_coverage, iter_embedded_characters
from .model import EmbeddedEvidence

POLICY_VERSION = "source-progress-v1"
GROUNDING_START_VERSION = "initial-grounding-v1"


class InitialGroundingProcessor:
    """Single-page startup rescue prefix, never a transcription hint.

    Constrain only the opening marker; the model still predicts the region type,
    coordinates, text and EOS. The pipeline excludes visually blank pages;
    multi-page starts are untouched. State belongs to one greedy generation only.
    """

    def __init__(self, tokenizer):
        self.prefix = tokenizer.encode("<|det|>", add_special_tokens=False)
        if not self.prefix or tokenizer.decode(self.prefix, skip_special_tokens=False) != "<|det|>":
            raise ValueError("tokenizer must round-trip the grounding marker")
        self.prefill = None
        self.seen = None

    def __call__(self, input_ids, logits):
        import mlx.core as mx

        if (input_ids.ndim > 1 and input_ids.shape[0] != 1) or (logits.ndim > 1 and logits.shape[0] != 1):
            raise ValueError("grounding prefix requires one decoding hypothesis")
        count = input_ids.size
        if self.seen is not None and count < self.seen:
            raise ValueError("grounding prefix state cannot be reused after rollback")
        self.seen = count
        if self.prefill is None:
            self.prefill = count
        index = count - self.prefill
        if index >= len(self.prefix):
            return logits
        token = self.prefix[index]
        if not 0 <= token < logits.shape[-1] or not mx.isfinite(logits[..., token]).all().item():
            raise ValueError("grounding prefix conflicts with the model or another hard constraint")
        return mx.where(mx.arange(logits.shape[-1]) == token, logits, -float("inf"))


WORD = re.compile(r"[^\W\d_]+|\d+", re.UNICODE)
ATOM = re.compile(r"\\[A-Za-z]+|[A-Za-z]+|\d+|[^\s]")
GREEK = dict(zip(
    "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau upsilon phi chi psi omega".split(),
    "αβγδεζηθικλμνξοπρστυφχψω",
))
MATH_WORDS = {"sin", "cos", "tan", "log", "exp", "max", "min", "lim", "sup", "inf", "det", "dim", "gcd", "lcm", "mod", "ker"}


def words(text: str) -> list[str]:
    text = re.sub(r"\\([A-Za-z]+)", lambda m: GREEK.get(m[1], " "), text)
    return WORD.findall(unicodedata.normalize("NFKC", text).casefold())


def structural_atoms(text: str) -> list[str]:
    # Grounding coordinates and table cells are not evidence of a text loop.
    text = re.sub(r"<\|(?:det|ref)\|>.*?(?:<\|/(?:det|ref)\|>|$)", " ", text, flags=re.S)
    text = re.sub(r"<table\b.*?(?:</table>|$)", " ", text, flags=re.S | re.I)
    text = re.sub(r"(?m)^\|.*$", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"<[^>]*(?:>|$)", " ", text)
    # Preserve literal numbers except in explicit index/enumeration positions.
    text = re.sub(r"([_^]\s*\{?)\s*\d+", r"\1 #", text)
    text = re.sub(r"\(\s*\d+\s*\)", "(#)", text)
    # Preserve the length of a spurious numeric hierarchy: collapsing the
    # entire chain to one token would hide 1.1.1.1... from the loop detector.
    text = re.sub(r"\b\d+(?:\.\d+){2,}\b", lambda m: re.sub(r"\d+", "#", m[0]), text)
    return ATOM.findall(text.casefold())


def loop_pattern(atoms: list[str]) -> tuple[list[str], int] | None:
    """Recognize a sustained periodic suffix, allowing changing counters."""
    sample = atoms[-256:]
    for size in range(1, min(48, len(sample) // 4) + 1):
        pattern = sample[-size:]
        required = max(4, math.ceil((48 if "#" in pattern else 24) / size))
        if len(sample) < required * size:
            continue
        if all(sample[-repeat * size : -(repeat - 1) * size] == pattern
               for repeat in range(2, required + 1)):
            return pattern, required
    return None


def structural_loop(text: str) -> bool:
    atoms = structural_atoms(text)
    # Check bounded overlapping windows, including the final suffix.
    return any(loop_pattern(atoms[max(0, end - 256):end])
               for end in [*range(48, len(atoms), 16), len(atoms)])


@dataclass
class SourceRegion:
    page: int
    bbox: tuple[float, float, float, float]
    tokens: list[str]
    cursor: int = 0
    steerable: bool = True
    bias_words: frozenset[str] = frozenset()


class SourceProgress:
    """Local occurrence alignment with bounded skips, not a bag-of-words bias."""

    def __init__(self, pages: list[EmbeddedEvidence]):
        self.regions: list[SourceRegion] = []
        self.page = 0
        self.active: int | None = None
        self.matched = 0
        self.emitted = 0
        self.disabled_regions = 0
        for page, evidence in enumerate(pages):
            if evidence.extractor == "ignored":
                continue
            for block in evidence.blocks:
                local = EmbeddedEvidence(text=str(block.get("text", "")), blocks=[block])
                box = block.get("bbox", [])
                chars = list(iter_embedded_characters(local))
                # Geometry is mandatory. Native text is assessed independently
                # of the very OCR output this guide is intended to correct.
                if (not assess_embedded(local).geometric or not chars
                        or len(box) != 4 or not all(math.isfinite(float(x)) for x in box)
                        or not (0 <= box[0] < box[2] <= 1000 and 0 <= box[1] < box[3] <= 1000)):
                    self.disabled_regions += 1
                    continue
                tokens = words(local.text)
                # Math-only regions may corroborate real repetitions, but their
                # flattened serialization is not used for positive token bias.
                prose = sum(len(t) >= 3 and t.isalpha() for t in tokens) >= 3
                bias_words = frozenset(t.casefold() for t in WORD.findall(unicodedata.normalize("NFKC", local.text))
                                       if len(t) >= 3 and t.isalpha() and not t.isupper() and t.casefold() not in MATH_WORDS)
                self.regions.append(SourceRegion(page, tuple(box), tokens, steerable=prose, bias_words=bias_words))

    def select_box(self, box) -> None:
        candidates = [(i, max(bbox_coverage(box, r.bbox), bbox_coverage(r.bbox, box)))
                      for i, r in enumerate(self.regions) if r.page == self.page]
        candidates = [(i, score) for i, score in candidates if score >= .5]
        self.active = max(candidates, key=lambda item: item[1])[0] if candidates else None

    def frontier(self, *, steering: bool = False, cursors: dict[int, int] | None = None) -> list[tuple[int, int, str, float]]:
        cursors = cursors or {}
        available = [(i, r) for i, r in enumerate(self.regions)
                     if r.page == self.page and cursors.get(i, r.cursor) < len(r.tokens) and (r.steerable or not steering)]
        if self.active is not None:
            available = [(i, r) for i, r in available if i == self.active]
        else:
            available = available[:2]  # modest reading-order alternatives
        result = []
        for i, region in available:
            start = cursors.get(i, region.cursor)
            for j in range(start, min(len(region.tokens), start + 12)):
                # Do not bias bare variables/numerals/acronyms over a LaTeX
                # opener, or skip across them to a later prose word. Otherwise
                # native text can flatten inline math before in_formula is set.
                if steering and region.tokens[j] not in region.bias_words:
                    break
                result.append((i, j, region.tokens[j], 1.0 / (1 + .25 * (j - start))))
        return result

    def consume(self, word: str) -> bool:
        self.emitted += 1
        matches = [item for item in self.frontier() if item[2] == word]
        if not matches:
            return False
        i, j, _, _ = max(matches, key=lambda item: item[3])
        self.regions[i].cursor = j + 1
        self.matched += 1
        return True

    def prefix_score(self, prefix: str) -> float:
        if not prefix:
            return 0.0
        return max((weight for _, _, word, weight in self.frontier(steering=True)
                    if word.startswith(prefix)), default=0.0)

    def extension_score(self, pending: str, addition: str) -> float:
        """Score subword extensions and a new word after a boundary, read-only."""
        cursors: dict[int, int] = {}
        reward = 0.0
        for match in WORD.finditer(pending + addition):
            prefix = unicodedata.normalize("NFKC", match[0]).casefold()
            frontier = self.frontier(steering=True, cursors=cursors)
            if match.end() > len(pending):
                reward = max(reward, max((weight for _, _, word, weight in frontier
                                         if word.startswith(prefix)), default=0.0))
            matches = [item for item in frontier if item[2] == prefix]
            if matches:
                i, j, _, _ = max(matches, key=lambda item: item[3])
                cursors[i] = j + 1
        return reward

    def after_word(self, word: str):
        matches = [item for item in self.frontier(steering=True) if item[2] == word]
        if not matches:
            return []
        i, j, _, _ = max(matches, key=lambda item: item[3])
        return self.frontier(steering=True, cursors={i: j + 1})

    def eos_penalty(self) -> float:
        remaining = sum(len(r.tokens) - r.cursor for r in self.regions if r.page >= self.page)
        # Never hard-ban EOS, and stop holding it back once alignment is lost.
        return 1.5 if remaining >= 8 and self.matched >= 3 and self.matched / max(1, self.emitted) > .5 else 0.0


class DecodeGuide:
    """Streaming surface parser plus source and structural scoring state."""

    def __init__(self, pages: list[EmbeddedEvidence]):
        self.source = SourceProgress(pages)
        self.pending = ""
        self.tail = ""
        self.hidden: str | None = None
        self.payload = ""
        self.in_table = False
        self.in_formula = False
        self.grounding_seen = False
        self.unsupported_region = False
        self.loop_steps = 0
        self.source_steps = 0
        self.steps = 0
        self.characters = 0
        self.last_supported_character = -1000
        self._loop_tail: str | None = None
        self._loop_atoms: list[str] = []
        self._loop_pattern = None

    def feed(self, text: str) -> None:
        self.characters += len(text)
        self.tail = (self.tail + text)[-8192:]
        self.pending += text
        while self.pending:
            if self.pending.startswith("<"):
                end = self.pending.find(">")
                if end < 0:
                    break
                tag, self.pending = self.pending[:end + 1], self.pending[end + 1:]
                if tag in {"<|det|>", "<|ref|>"}:
                    self.hidden, self.payload = tag, ""
                elif tag in {"<|/det|>", "<|/ref|>"}:
                    if self.hidden == "<|det|>":
                        coords = re.findall(r"-?\d+(?:\.\d+)?", self.payload)
                        if len(coords) == 4:
                            box = tuple(map(float, coords))
                            self.source.select_box(box)
                        else:
                            self.source.active = None
                        self.unsupported_region = self.source.active is None
                        self.grounding_seen = True
                        self.in_formula = bool(re.search(r"formula|equation", self.payload, re.I))
                    self.hidden, self.payload = None, ""
                elif tag == "<PAGE>":
                    # The model may emit a leading PAGE marker.
                    if self.source.emitted or self.grounding_seen:
                        self.source.page += 1
                    self.source.active = None
                    self.unsupported_region = False
                    self.grounding_seen = False
                    self.in_formula = False
                elif tag.lower().startswith("<table"):
                    self.in_table = True
                elif tag.lower() == "</table>":
                    self.in_table = False
                continue
            if self.hidden:
                end = self.pending.find("<")
                if end < 0:
                    self.payload = (self.payload + self.pending)[-2048:]
                    self.pending = ""
                else:
                    self.payload += self.pending[:end]
                    self.pending = self.pending[end:]
                continue
            if self.pending.startswith("\\"):
                match = re.match(r"\\([A-Za-z]+|.)", self.pending, re.S)
                if not match or (match.end() == len(self.pending) and match[1].isalpha()):
                    break
                command = match[1]
                self.pending = self.pending[match.end():]
                if command in {"(", "["}:
                    self.in_formula = True
                elif command in {")", "]"}:
                    self.in_formula = False
                elif command in GREEK and self.grounding_seen and not self.unsupported_region:
                    if self.source.consume(GREEK[command]):
                        self.last_supported_character = self.characters - len(self.pending)
                continue
            match = WORD.match(self.pending)
            if match:
                if match.end() == len(self.pending):
                    break  # incomplete word; candidates may extend it
                if self.grounding_seen and not self.in_table and not self.unsupported_region:
                    if self.source.consume(unicodedata.normalize("NFKC", match[0]).casefold()):
                        self.last_supported_character = self.characters - len(self.pending) + match.end()
                self.pending = self.pending[match.end():]
            else:
                self.pending = self.pending[1:]
        # Malformed markup/one gigantic token must not grow parser state forever.
        self.pending = self.pending[-4096:]

    def score(self, addition: str, *, eos: bool = False) -> float:
        if eos:
            return -self.source.eos_penalty()
        score = 0.0
        if self.grounding_seen and not (self.hidden or self.in_table or self.in_formula or self.unsupported_region):
            if not (self.pending + addition).lstrip().startswith(("<", "\\")):
                score += 1.5 * self.source.extension_score(self.pending, addition)
        if len(self.tail) >= 512 and not self.in_table and not self.recent_source_progress:
            before, pattern = self.loop_context()
            if pattern:
                after = structural_atoms(self.tail + addition)[-256:]
                # Penalize continuing the attractor, including a partial atom;
                # a matching new source occurrence takes precedence.
                if (after == before or loop_pattern(after)) and score <= 0:
                    score -= 6.0
        return score

    def loop_context(self):
        if self._loop_tail != self.tail:
            self._loop_tail = self.tail
            self._loop_atoms = structural_atoms(self.tail)[-256:]
            self._loop_pattern = loop_pattern(self._loop_atoms) if len(self.tail) >= 512 else None
        return self._loop_atoms, self._loop_pattern

    @property
    def recent_source_progress(self) -> bool:
        return self.characters - self.last_supported_character <= 64

    def diagnostics(self) -> dict[str, object]:
        return {"policy": POLICY_VERSION, "source_regions": len(self.source.regions),
                "disabled_regions": self.source.disabled_regions,
                "matched_occurrences": self.source.matched,
                "emitted_words": self.source.emitted,
                "source_bias_steps": self.source_steps, "loop_bias_steps": self.loop_steps,
                "steps": self.steps, "method": "bounded_candidate_bias", "top_k": 16,
                "confidence_basis": "post_constraint_decoder_distribution"}


class DecodeLogitsProcessor:
    """MLX adapter. All state belongs to one invocation; prefill is not OCR text."""

    def __init__(self, tokenizer, pages: list[EmbeddedEvidence]):
        self.tokenizer = tokenizer
        self.guide = DecodeGuide(pages)
        self.seen: int | None = None
        self.pending_ids: list[int] = []
        eos_ids = getattr(tokenizer, "eos_token_ids", []) or []
        self.eos_ids = {eos_ids} if isinstance(eos_ids, int) else set(eos_ids)
        eos = getattr(tokenizer, "eos_token_id", None)
        if isinstance(eos, int):
            self.eos_ids.add(eos)

    def decode(self, ids) -> str:
        return self.tokenizer.decode(ids, skip_special_tokens=False, clean_up_tokenization_spaces=False)

    def __call__(self, input_ids, logits):
        import mlx.core as mx

        if logits.ndim > 1 and logits.shape[0] != 1:
            raise ValueError("decode guide requires one state per hypothesis")
        ids = input_ids.reshape(-1)
        count = ids.size
        if self.seen is not None:
            if count < self.seen:
                raise ValueError("decode guide cannot reuse state after cache rollback")
            self.pending_ids.extend(ids[self.seen:].tolist())
        self.seen = count
        pending = self.decode(self.pending_ids) if self.pending_ids else ""
        # Flush complete UTF-8 immediately; the surface parser retains partial
        # words/commands. Waiting for a trailing space would delay source state
        # by an entire sentence with leading-space BPE tokens.
        if pending and "\ufffd" not in pending:
            self.guide.feed(pending)
            self.pending_ids.clear()
            pending = ""
        scores = logits.astype(mx.float32)
        source_active = self.guide.grounding_seen and not (
            self.guide.hidden or self.guide.in_formula or self.guide.in_table or self.guide.unsupported_region
        ) and bool(self.guide.source.frontier(steering=True))
        loop_active = not self.guide.in_table and not self.guide.recent_source_progress and bool(self.guide.loop_context()[1])
        if not (source_active or loop_active or self.guide.source.eos_penalty()):
            self.guide.steps += 1
            return scores
        row = scores.reshape(-1)
        k = min(16, row.size)
        candidates = set(mx.argpartition(-row, kth=k - 1)[:k].tolist())
        # Native candidates may be outside top-k. Only add short local prefixes;
        # their bounded bonus cannot override an arbitrarily stronger image model.
        if source_active:
            prefix = (self.guide.pending + pending).strip().casefold()
            for _, _, word, _ in self.guide.source.frontier(steering=True)[:12]:
                if prefix and not word.startswith(prefix):
                    continue
                suffix = word[len(prefix):] if prefix else word
                for text in (suffix, " " + suffix) if not prefix else (suffix,):
                    encoded = self.tokenizer.encode(text, add_special_tokens=False)
                    if encoded:
                        candidates.add(int(encoded[0]))
            for _, _, word, _ in self.guide.source.after_word(prefix)[:12]:
                encoded = self.tokenizer.encode(" " + word, add_special_tokens=False)
                if encoded:
                    candidates.add(int(encoded[0]))
        candidates.update(self.eos_ids)
        indices, adjustments = [], []
        for token in sorted(candidates):
            if not 0 <= token < row.size:
                continue
            addition = self.decode([*self.pending_ids, token])
            bias = self.guide.score(addition, eos=token in self.eos_ids)
            if bias:
                indices.append(token)
                adjustments.append(bias)
        if adjustments:
            self.guide.source_steps += any(value > 0 for value in adjustments)
            self.guide.loop_steps += any(value <= -6 for value in adjustments)
            row = row.at[mx.array(indices)].add(mx.array(adjustments, dtype=mx.float32))
        self.guide.steps += 1
        return row.reshape(scores.shape)
