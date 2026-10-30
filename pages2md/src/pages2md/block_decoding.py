"""Automatic greedy grounding grammar and evidence-dependent block progress.

Only syntax is hard constrained. Content similarity alone never forbids text.
Retry uses fresh generation state and exact token-prefix replay, not KV-cache
mutation. Generated geometry is a diagnostic, not independent ground truth.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
import re

VERSION = "grounded-blocks-v1"
OPEN, CLOSE = "<|det|>", "<|/det|>"
HEADER = re.compile(r"<\|det\|>\s*([\w -]+?)\s*\[\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\]\s*<\|/det\|>")


def _number_prefix(digits: str, minimum: int, maximum: int) -> bool:
    if minimum > maximum:
        return False
    if not digits:
        return True
    if len(digits) > 4 or (len(digits) > 1 and digits[0] == "0"):
        return False
    value = int(digits)
    return any(value * 10**k <= maximum and (value + 1) * 10**k - 1 >= minimum
               for k in range(4 - len(digits) + 1) if digits != "0" or k == 0)


@lru_cache(maxsize=8192)
def valid_header_prefix(payload: str) -> bool:
    """Does this string extend to a legal integer-coordinate block header?"""
    label, bracket, rest = payload.partition("[")
    if not re.fullmatch(r"\s*[A-Za-z_][A-Za-z_0-9 -]*", label):
        return not bracket and not label.strip()
    if not bracket:
        return True
    values = []
    for index in range(4):
        match = re.match(r"\s*(\d*)(\s*)", rest)
        if not match:
            return False
        digits, spaces = match.groups()
        minimum = values[index - 2] + 1 if index >= 2 else 0
        maximum = 999 if index < 2 else 1000
        if not _number_prefix(digits, minimum, maximum):
            return False
        rest = rest[match.end():]
        complete = bool(digits) and minimum <= int(digits) <= maximum
        if not rest:
            return complete if spaces else True
        separator = "," if index < 3 else "]"
        if not complete or not rest.startswith(separator):
            return False
        values.append(int(digits))
        rest = rest[1:]
    rest = rest.lstrip()
    return CLOSE.startswith(rest) or rest.startswith(CLOSE)


@dataclass(frozen=True)
class RegionBlock:
    start: int
    body_start: int
    end: int
    kind: str
    box: tuple[int, int, int, int]
    text: str


def blocks(text: str) -> list[RegionBlock]:
    matches = list(HEADER.finditer(text))
    return [RegionBlock(m.start(), m.end(), matches[i + 1].start() if i + 1 < len(matches) else len(text),
                        m[1].strip(), tuple(int(m[j]) for j in range(2, 6)),
                        text[m.end():matches[i + 1].start() if i + 1 < len(matches) else len(text)])
            for i, m in enumerate(matches)]


def fingerprint(text: str) -> str:
    # Preserve actual letters, values, operators, and sub/superscript markers.
    # This projection is for comparison only; never rewrite the transcription.
    text = re.sub(r"\\(?:begin|end)\{(?:array|aligned|align\*?|gathered)\}(?:\{[lcr ]+\})?", "", text)
    text = re.sub(r"\\(?:mathrm|mathbf|mathbb|mathcal|mathsf|text|operatorname|left|right|big|Big|bigg|Bigg)\b\*?", "", text)
    return re.sub(r"[\s{}\\&]", "", text)


def area(box):
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def intersection(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def region_coverage(reference, candidate, tolerance=3):
    """Allow sub-percent coordinate jitter, not missing source regions."""
    padded = (max(0, candidate[0] - tolerance), max(0, candidate[1] - tolerance),
              min(1000, candidate[2] + tolerance), min(1000, candidate[3] + tolerance))
    return intersection(reference, padded) / max(1, area(reference))


def geometry_risk(previous: RegionBlock, current: RegionBlock) -> str | None:
    a, b = previous.box, current.box
    if min(area(a), area(b)) <= 0:
        return None
    overlap = intersection(a, b) / max(1, area(a) + area(b) - intersection(a, b))
    if overlap >= .55:
        return "same_region"
    horizontal = max(0, min(a[2], b[2]) - max(a[0], b[0])) / max(1, min(a[2]-a[0], b[2]-b[0]))
    if 0 <= b[1] - a[3] <= 24 and area(b) < .35 * area(a) and horizontal >= .7:
        return "compressed_near_copy"
    return None


def duplicate_blocks(text: str) -> list[dict]:
    regions = blocks(text)
    found = []
    for i, current in enumerate(regions):
        if current.kind not in {"text", "equation", "formula", "paragraph"} or "<table" in current.text.lower():
            continue
        value = fingerprint(current.text)
        if len(value) < 96:
            continue
        for j in range(max(0, i - 6), i):
            previous = regions[j]
            if "<PAGE>" in text[previous.start:current.start] or previous.kind != current.kind:
                continue
            reason = geometry_risk(previous, current)
            other = fingerprint(previous.text)
            if not reason or not .6 <= len(value) / max(1, len(other)) <= 1.65:
                continue
            similarity = SequenceMatcher(None, other, value, autojunk=False).ratio()
            if similarity >= .78:
                found.append({"block": i, "previous": j, "reason": reason,
                              "similarity": round(similarity, 4), "start": current.start})
                break
    return found


class BlockLogitsProcessor:
    """Single-hypothesis streaming grammar, completed blocks, and soft steering."""

    def __init__(self, tokenizer):
        self.tokenizer = tokenizer
        self.seen = None
        self.ids: list[int] = []
        self.ends: list[int] = []
        self.text = ""
        self.grammar_steps = 0
        self.bias_steps = 0
        self._pieces = {}
        self._risk = None
        self.model_scores: list[float] = []
        self._distribution = None
        eos = getattr(tokenizer, "eos_token_ids", []) or []
        self.eos = {eos} if isinstance(eos, int) else set(eos)
        token = getattr(tokenizer, "eos_token_id", None)
        if isinstance(token, int):
            self.eos.add(token)

    def decode(self, ids):
        return self.tokenizer.decode(ids, skip_special_tokens=False, clean_up_tokenization_spaces=False)

    def _piece(self, token):
        if token not in self._pieces:
            self._pieces[token] = self.decode([token])
        return self._pieces[token]

    def __call__(self, input_ids, logits):
        import mlx.core as mx
        if (input_ids.ndim > 1 and input_ids.shape[0] != 1) or (logits.ndim > 1 and logits.shape[0] != 1):
            raise ValueError("block decoding requires one hypothesis")
        ids = input_ids.reshape(-1)
        if self.seen is not None:
            if ids.size < self.seen:
                raise ValueError("block decoding requires fresh state after rollback")
            added = ids[self.seen:].tolist()
            for token in added:
                if self._distribution is not None and len(added) == 1:
                    self.model_scores.append(float(self._distribution[token].item()))
                else:
                    self.model_scores.append(float("nan"))
                self.ids.append(token)
                self.text = self.decode(self.ids)
                self.ends.append(len(self.text))
        self.seen = ids.size
        row = logits.astype(mx.float32).reshape(-1)
        self._distribution = row - mx.logsumexp(row)
        start = self.text.rfind(OPEN)
        payload = self.text[start + len(OPEN):] if start >= 0 else ""
        header = start >= 0 and CLOSE not in payload
        if header:
            self._risk = None
            # Greedy-only: widen until a legal token occurs. All retained tokens
            # are legal; no unexamined token can beat the best legal candidate.
            count = min(32, row.size)
            while True:
                top = mx.argpartition(-row, kth=count - 1)[:count].tolist()
                valid = [t for t in top if t not in self.eos and self._piece(t)
                         and valid_header_prefix(payload + self._piece(t))]
                if valid and mx.isfinite(row[mx.array(valid)]).any().item():
                    mask = mx.full(row.shape, -float("inf"))
                    mask[mx.array(valid)] = row[mx.array(valid)]
                    row = mask
                    self.grammar_steps += 1
                    break
                if count == row.size:
                    raise ValueError("grounding grammar has no legal finite continuation")
                count = min(row.size, count * 4)
        elif len(self.ids) % 8 == 0:
            self._risk = None
            regions = blocks(self.text)
            if regions:
                current = regions[-1]
                value = fingerprint(current.text)
                if (current.kind in {"equation", "formula", "text", "paragraph"} and len(value) >= 96
                        and "<table" not in current.text.lower()):
                    for previous in reversed(regions[-7:-1]):
                        if previous.kind != current.kind or "<PAGE>" in self.text[previous.start:current.start]:
                            continue
                        other = fingerprint(previous.text)
                        if geometry_risk(previous, current) and other.startswith(value):
                            self._risk = other
                            break
        if not header and self._risk:
            regions = blocks(self.text)
            value = fingerprint(regions[-1].text) if regions else ""
            if self._risk.startswith(value) and len(value) < len(self._risk):
                top = mx.argpartition(-row, kth=min(15, row.size - 1))[:16].tolist()
                penalized = [t for t in top if (piece := fingerprint(self._piece(t))) and self._risk[len(value):].startswith(piece)]
                if penalized:
                    row = row.at[mx.array(penalized)].add(-2.0)
                    self.bias_steps += 1
        return row.reshape(logits.shape)

    def fork(self, issue: dict) -> int | None:
        """Fork at the first substantive body token, preserving opening math."""
        region = blocks(self.text)[issue["block"]]
        skip = re.match(r"\s*(?:\\\[|\\\()?\s*(?:\\leq\b|\\geq\b|=)?\s*", region.text)
        offset = region.body_start + skip.end()
        index = bisect_right(self.ends, offset)
        return index if index < len(self.ids) else None

    def diagnostics(self):
        return {"policy": VERSION, "grammar_steps": self.grammar_steps, "bias_steps": self.bias_steps,
                "duplicates": duplicate_blocks(self.text), "completed_blocks": max(0, len(blocks(self.text)) - 1),
                "likelihood_basis": "before_block_constraints_after_other_processors"}


class ReplayBranchProcessor:
    """Rebuild a checkpoint by exact replay, then exclude one explored branch."""

    def __init__(self, prefix: list[int], banned: set[int]):
        self.prefix, self.banned = list(prefix), set(banned)
        self.prefill = self.seen = None

    def __call__(self, input_ids, logits):
        import mlx.core as mx
        if (input_ids.ndim > 1 and input_ids.shape[0] != 1) or (logits.ndim > 1 and logits.shape[0] != 1):
            raise ValueError("replay requires one hypothesis")
        if self.seen is not None and input_ids.size < self.seen:
            raise ValueError("replay requires fresh state")
        self.seen = input_ids.size
        if self.prefill is None:
            self.prefill = input_ids.size
        index = input_ids.size - self.prefill
        row = logits.astype(mx.float32).reshape(-1)
        if index < len(self.prefix):
            token = self.prefix[index]
            if not 0 <= token < row.size or not mx.isfinite(row[token]).item():
                raise ValueError("replay prefix conflicts with a hard constraint")
            return mx.where(mx.arange(row.size) == token, row, -float("inf")).reshape(logits.shape)
        if index == len(self.prefix):
            row[mx.array(sorted(self.banned))] = -float("inf")
            if not mx.isfinite(row).any().item():
                raise ValueError("replay has no unexplored continuation")
        return row.reshape(logits.shape)
