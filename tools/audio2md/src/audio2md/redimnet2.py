from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import cache
import hashlib
import math
from pathlib import Path
import subprocess
from typing import Any
from urllib.request import Request, urlopen

from .model import EmbeddingSample, Segment, SpeakerProfile

REDIMNET2_MODEL = "PalabraAI/ReDimNet2-B6-vb2+vox2_v0-lm"
REDIMNET2_REPOSITORY = "PalabraAI/redimnet2"
REDIMNET2_REVISION = "cdc875670034dd7068013ca2ab21ec083a040ff8"
REDIMNET2_RELEASE = "v1.0.0"
REDIMNET2_CHECKPOINT = "b6-vb2+vox2_v0-lm.pt"
REDIMNET2_CHECKPOINT_URL = (
    "https://github.com/PalabraAI/redimnet2/releases/download/"
    "v1.0.0/b6-vb2%2Bvox2_v0-lm.pt"
)
REDIMNET2_CHECKPOINT_SHA256 = "e0a7d340a92f798720d1208949aa6a6bd0cddcb0ba7d4cec33596a17a484e6a2"
REDIMNET2_DIMENSION = 192
REDIMNET2_SAMPLE_RATE = 16_000

MIN_SAMPLE_SECONDS = 2.0
MAX_SAMPLE_SECONDS = 6.0
MAX_SAMPLES_PER_SPEAKER = 4
DEFAULT_SIMILARITY_THRESHOLD = 0.65
DEFAULT_SIMILARITY_MARGIN = 0.08


@dataclass(frozen=True)
class CleanInterval:
    speaker: str
    start: float
    end: float
    window: int

    @property
    def duration_seconds(self) -> float:
        return self.end - self.start


def normalize_embedding(values: Any) -> list[float]:
    vector = [float(value) for value in values]
    if len(vector) != REDIMNET2_DIMENSION:
        raise ValueError(
            f"ReDimNet2 returned {len(vector)} dimensions; expected {REDIMNET2_DIMENSION}"
        )
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("ReDimNet2 returned a zero or non-finite embedding")
    return [value / norm for value in vector]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != REDIMNET2_DIMENSION or len(right) != REDIMNET2_DIMENSION:
        raise ValueError("cannot compare embeddings with an unexpected dimension")
    return sum(first * second for first, second in zip(left, right, strict=True))


def select_clean_intervals(
    segments: list[Segment],
    *,
    window: int,
    minimum_seconds: float = MIN_SAMPLE_SECONDS,
    maximum_seconds: float = MAX_SAMPLE_SECONDS,
    maximum_samples: int = MAX_SAMPLES_PER_SPEAKER,
) -> dict[str, list[CleanInterval]]:
    """Select non-overlapping voice samples, spread across one MOSS window."""
    if minimum_seconds <= 0 or maximum_seconds < minimum_seconds or maximum_samples <= 0:
        raise ValueError("invalid ReDimNet2 speech-sample limits")

    candidates: dict[str, list[CleanInterval]] = {}
    for segment in segments:
        if segment.end - segment.start < minimum_seconds:
            continue
        if any(
            other.speaker != segment.speaker
            and min(segment.end, other.end) > max(segment.start, other.start)
            for other in segments
        ):
            continue
        start = segment.start
        while segment.end - start >= minimum_seconds:
            end = min(segment.end, start + maximum_seconds)
            candidates.setdefault(segment.speaker, []).append(CleanInterval(
                speaker=segment.speaker,
                start=start,
                end=end,
                window=window,
            ))
            start = end

    selected: dict[str, list[CleanInterval]] = {}
    for speaker, intervals in candidates.items():
        intervals.sort(key=lambda item: (item.start + item.end) / 2)
        if len(intervals) <= maximum_samples:
            selected[speaker] = intervals
            continue
        indexes = {
            round(index * (len(intervals) - 1) / (maximum_samples - 1))
            for index in range(maximum_samples)
        } if maximum_samples > 1 else {len(intervals) // 2}
        selected[speaker] = [intervals[index] for index in sorted(indexes)]
    return selected


def extract_window_evidence(
    path: Path,
    segments: list[Segment],
    *,
    window: int,
    source_track: str,
    embedder: Any,
) -> tuple[dict[str, list[EmbeddingSample]], list[dict[str, Any]]]:
    intervals = select_clean_intervals(segments, window=window)
    evidence: dict[str, list[EmbeddingSample]] = {}
    diagnostics: list[dict[str, Any]] = []
    for speaker, speaker_intervals in intervals.items():
        for interval in speaker_intervals:
            try:
                vector = normalize_embedding(embedder.embed(path, interval.start, interval.end))
            except Exception as error:
                raise RuntimeError(
                    "ReDimNet2 inference failed for "
                    f"{path.name} at {interval.start:.3f}-{interval.end:.3f}s: {error}"
                ) from error
            sample = EmbeddingSample(
                vector=vector,
                source_track=source_track,
                start=interval.start,
                end=interval.end,
                duration_seconds=interval.duration_seconds,
                window=window,
                quality={
                    "overlap_free": True,
                    "minimum_duration_met": True,
                    "l2_norm": math.sqrt(sum(value * value for value in vector)),
                },
            )
            evidence.setdefault(speaker, []).append(sample)
            diagnostics.append({
                "local_speaker": speaker,
                "start": interval.start,
                "end": interval.end,
                "duration_seconds": interval.duration_seconds,
                "embedding_dimension": len(vector),
                "l2_norm": sample.quality["l2_norm"],
            })
    return evidence, diagnostics


def reconcile_speakers(
    by_window: list[list[Segment]],
    evidence_by_window: list[dict[str, list[EmbeddingSample]]],
    *,
    role: str,
    profiles: dict[str, SpeakerProfile] | None = None,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    similarity_margin: float = DEFAULT_SIMILARITY_MARGIN,
) -> tuple[dict[str, str], list[dict[str, Any]], dict[str, SpeakerProfile]]:
    if role == "microphone":
        return {
            segment.speaker: "Remco"
            for segments in by_window
            for segment in segments
        }, [], profiles if profiles is not None else {}
    if len(by_window) != len(evidence_by_window):
        raise ValueError("speaker evidence does not match the number of MOSS windows")

    meeting_profiles = profiles if profiles is not None else {}
    mapping: dict[str, str] = {}
    decisions: list[dict[str, Any]] = []
    next_speaker = _next_speaker_number(meeting_profiles)

    for window_index, (segments, window_evidence) in enumerate(
        zip(by_window, evidence_by_window, strict=True), 1
    ):
        local_speakers = sorted({segment.speaker for segment in segments})
        scored: dict[str, list[tuple[float, str]]] = {}
        eligible: list[tuple[float, str, str]] = []
        for local in local_speakers:
            samples = window_evidence.get(local, [])
            scores = sorted(
                (
                    (_profile_similarity(samples, profile.samples), speaker)
                    for speaker, profile in meeting_profiles.items()
                    if samples and profile.samples
                ),
                reverse=True,
            )
            scored[local] = scores
            if not scores:
                continue
            best_score, best_speaker = scores[0]
            second_score = scores[1][0] if len(scores) > 1 else None
            margin = best_score - second_score if second_score is not None else math.inf
            if best_score >= similarity_threshold and margin >= similarity_margin:
                eligible.append((best_score, local, best_speaker))

        assigned_local: set[str] = set()
        assigned_meeting: set[str] = set()
        for score, local, speaker in sorted(eligible, reverse=True):
            if local in assigned_local or speaker in assigned_meeting:
                continue
            mapping[local] = speaker
            assigned_local.add(local)
            assigned_meeting.add(speaker)
            meeting_profiles[speaker].samples.extend(window_evidence[local])

        for local in local_speakers:
            scores = scored[local]
            best_score = scores[0][0] if scores else None
            best_speaker = scores[0][1] if scores else None
            second_score = scores[1][0] if len(scores) > 1 else None
            score_margin = (
                best_score - second_score
                if best_score is not None and second_score is not None
                else None
            )
            if local in assigned_local:
                assigned = mapping[local]
                decision = "matched"
                reason = "threshold_and_margin_met"
            else:
                assigned = f"Speaker {next_speaker}"
                next_speaker += 1
                mapping[local] = assigned
                samples = list(window_evidence.get(local, []))
                meeting_profiles[assigned] = SpeakerProfile(
                    speaker=assigned,
                    model=REDIMNET2_MODEL,
                    model_revision=REDIMNET2_REVISION,
                    checkpoint_sha256=REDIMNET2_CHECKPOINT_SHA256,
                    embedding_dimension=REDIMNET2_DIMENSION,
                    samples=samples,
                )
                decision = "new"
                if not samples:
                    reason = "no_clean_embedding"
                elif best_score is None:
                    reason = "no_existing_profile"
                elif best_score < similarity_threshold:
                    reason = "below_similarity_threshold"
                elif score_margin is not None and score_margin < similarity_margin:
                    reason = "ambiguous_margin"
                else:
                    reason = "one_to_one_conflict"
            decisions.append({
                "window": window_index,
                "local_speaker": local,
                "speaker": assigned,
                "decision": decision,
                "reason": reason,
                "similarity_threshold": similarity_threshold,
                "similarity_margin": similarity_margin,
                "best_candidate": best_speaker,
                "best_score": best_score,
                "second_best_score": second_score,
                "score_margin": score_margin,
                "scores": [
                    {"speaker": speaker, "cosine_similarity": score}
                    for score, speaker in scores
                ],
                "sample_count": len(window_evidence.get(local, [])),
            })
    return mapping, decisions, meeting_profiles


def _profile_similarity(samples: list[EmbeddingSample], profile: list[EmbeddingSample]) -> float:
    """Average each new sample's best match against retained profile evidence."""
    return sum(
        max(cosine_similarity(sample.vector, known.vector) for known in profile)
        for sample in samples
    ) / len(samples)


def _next_speaker_number(profiles: dict[str, SpeakerProfile]) -> int:
    numbers = []
    for speaker in profiles:
        prefix, separator, suffix = speaker.rpartition(" ")
        if separator and prefix == "Speaker" and suffix.isdigit():
            numbers.append(int(suffix))
    return max(numbers, default=0) + 1


class ReDimNet2Embedder:
    """Pinned ReDimNet2 B6 inference for 16 kHz mono speech intervals."""

    def __init__(self) -> None:
        self._model: Any | None = None
        self._torch: Any | None = None

    def embed(self, path: Path, start: float, end: float) -> list[float]:
        model, torch = self._load()
        duration = end - start
        process = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-v", "error", "-ss", str(start),
                "-t", str(duration), "-i", str(path), "-map", "0:a:0",
                "-ac", "1", "-ar", str(REDIMNET2_SAMPLE_RATE),
                "-f", "f32le", "pipe:1",
            ],
            check=True,
            capture_output=True,
        )
        try:
            import numpy as np
        except ImportError as error:
            raise RuntimeError("NumPy is unavailable in the audio2md environment") from error
        waveform = np.frombuffer(process.stdout, dtype="<f4").copy()
        if len(waveform) < int(MIN_SAMPLE_SECONDS * REDIMNET2_SAMPLE_RATE):
            raise RuntimeError("decoded speech interval is shorter than two seconds")
        with torch.inference_mode():
            result = model(torch.from_numpy(waveform).unsqueeze(0)).squeeze(0).cpu().tolist()
        return normalize_embedding(result)

    def _load(self) -> tuple[Any, Any]:
        if self._model is not None and self._torch is not None:
            return self._model, self._torch
        try:
            import torch
        except ImportError as error:
            raise RuntimeError(
                "ReDimNet2 runtime is unavailable; run audio2md through its locked environment"
            ) from error
        try:
            _ensure_verified_checkpoint(torch)
            model = torch.hub.load(
                f"{REDIMNET2_REPOSITORY}:{REDIMNET2_REVISION}",
                "redimnet2",
                model_name="b6",
                train_type="lm",
                dataset="vb2+vox2_v0",
                pretrained=True,
                source="github",
                trust_repo=True,
                skip_validation=True,
                verbose=False,
            )
            model.eval()
        except Exception as error:
            raise RuntimeError(
                "could not load pinned ReDimNet2 B6 "
                f"({REDIMNET2_REVISION}, checkpoint {REDIMNET2_CHECKPOINT_SHA256}): {error}"
            ) from error
        self._model = model
        self._torch = torch
        return model, torch


def _ensure_verified_checkpoint(torch: Any) -> Path:
    checkpoint = Path(torch.hub.get_dir()) / "checkpoints" / REDIMNET2_CHECKPOINT
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    if checkpoint.exists():
        actual = _sha256(checkpoint)
        if actual != REDIMNET2_CHECKPOINT_SHA256:
            raise RuntimeError(
                f"cached checkpoint checksum mismatch at {checkpoint}: "
                f"expected {REDIMNET2_CHECKPOINT_SHA256}, got {actual}"
            )
        return checkpoint

    temporary = checkpoint.with_suffix(checkpoint.suffix + ".part")
    request = Request(REDIMNET2_CHECKPOINT_URL, headers={"User-Agent": "audio2md"})
    try:
        with urlopen(request) as response, temporary.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        actual = _sha256(temporary)
        if actual != REDIMNET2_CHECKPOINT_SHA256:
            raise RuntimeError(
                f"downloaded checkpoint checksum mismatch: expected "
                f"{REDIMNET2_CHECKPOINT_SHA256}, got {actual}"
            )
        temporary.replace(checkpoint)
    finally:
        temporary.unlink(missing_ok=True)
    return checkpoint


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@cache
def get_redimnet2_embedder() -> ReDimNet2Embedder:
    return ReDimNet2Embedder()


def profile_diagnostics(profiles: dict[str, SpeakerProfile]) -> dict[str, Any]:
    return {
        speaker: {
            "model": profile.model,
            "model_revision": profile.model_revision,
            "checkpoint_sha256": profile.checkpoint_sha256,
            "embedding_dimension": profile.embedding_dimension,
            "sample_count": len(profile.samples),
            "samples": [
                {key: value for key, value in asdict(sample).items() if key != "vector"}
                for sample in profile.samples
            ],
        }
        for speaker, profile in profiles.items()
    }
