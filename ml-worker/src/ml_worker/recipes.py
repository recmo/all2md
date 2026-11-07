"""Implement only fixed, locally installed recipes; job data is never executable."""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
import subprocess

from . import audio, text
from .client import digest, local_path

LEVELS_RECIPE = "audio-levels-v1"
SPEECH_RECIPE = "speech2md-v1"
RECIPES = (SPEECH_RECIPE, audio.RECIPE, LEVELS_RECIPE, text.EMBED_RECIPE, text.RERANK_RECIPE)


def json_output(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def validate_assignment(assignment, root):
    job = assignment["job"]
    if job["recipe"] not in RECIPES:
        raise ValueError("unknown recipe")
    source = local_path(root, job["source"])
    inputs = {local_path(root, path) for path in assignment["inputs"]}
    outputs = [local_path(root, path) for path in assignment["outputs"]]
    if not outputs or len(set(outputs)) != len(outputs) or source in outputs or source in inputs or set(outputs) & inputs:
        raise ValueError("output paths must be unique and independent of inputs/source")
    return source, inputs, outputs


def _speech(assignment, root, source, inputs, outputs):
    import yaml
    raw = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", source.read_text(encoding="utf-8"), re.S)
    if not raw:
        raise ValueError("recording requires YAML frontmatter")
    metadata = yaml.safe_load(raw[1])
    if not isinstance(metadata, dict):
        raise ValueError("recording frontmatter must be a mapping")
    refs = [metadata[k] for k in ("audio", "manifest") if k in metadata]
    if len(refs) != 1 or not isinstance(refs[0], str):
        raise ValueError("recording requires one audio or manifest")
    def assigned(parent, name):
        if not isinstance(name, str):
            raise ValueError("input reference must be a path")
        target = local_path(root, str((parent / name).relative_to(root)))
        if target not in inputs:
            raise ValueError("recording references an unassigned input")
        return target
    reference = assigned(source.parent, refs[0])
    if "manifest" in metadata:
        manifest = json.loads(reference.read_text())
        if manifest.get("schemaVersion") in {2, 3}:
            assigned(reference.parent, manifest["container"]["file"])
        elif manifest.get("schemaVersion") == 1:
            for track in manifest["audio"]:
                assigned(reference.parent, track["file"])
        else:
            raise ValueError("unsupported meeting capture schema version")
    transcript = source.with_name("transcript.md") if source.name == "recording.md" else source.with_suffix(".transcript.md")
    if set(outputs) != {transcript, transcript.with_suffix(".voiceprints.json")}:
        raise ValueError("speech output paths do not match the recording")
    # The job lock protects this shared cache. speech2md validates audio, model,
    # hotwords and inference settings before using any saved generation.
    moss = transcript.with_suffix(".moss.npz")
    cached = root.parent / "speech.moss.npz"
    if not moss.exists() and cached.is_file():
        shutil.copyfile(cached, moss)
    try:
        subprocess.run(["speech2md", str(source), "--force"], check=True, stdin=subprocess.DEVNULL)
    finally:
        if moss.is_file():
            temporary = cached.with_suffix(".part")
            shutil.copyfile(moss, temporary)
            temporary.replace(cached)


def execute(assignment, root, *, scratch=None):
    source, inputs, outputs = validate_assignment(assignment, root)
    job = assignment["job"]
    recipe = job["recipe"]
    if recipe == SPEECH_RECIPE:
        _speech(assignment, root, source, inputs, outputs)
        return
    parameters = assignment.get("parameters", {}).get("ml")
    if not isinstance(parameters, dict):
        raise ValueError("recipe requires an ml mapping selected by fields: [ml]")
    allowed = {
        text.EMBED_RECIPE: {"texts", "input_type", "namespace"},
        text.RERANK_RECIPE: {"texts", "query", "top_n"},
        audio.RECIPE: {"tracks", "denoise", "levels", "attenuation_db"},
        LEVELS_RECIPE: {"tracks", "denoise", "levels"},
    }[recipe]
    if set(parameters) - allowed:
        raise ValueError("unknown recipe parameters: " + ", ".join(sorted(set(parameters)-allowed)))
    provenance = {"recipe": recipe, "fingerprint": job["fingerprint"],
                  "source": job["source"], "source_revision": job["source_revision"]}
    if recipe in {text.EMBED_RECIPE, text.RERANK_RECIPE}:
        if len(outputs) != 1 or outputs[0].suffix != ".json":
            raise ValueError("text recipes require exactly one JSON output")
        result = (text.embeddings if recipe == text.EMBED_RECIPE else text.rerank)(parameters)
        result["provenance"] = provenance
        json_output(outputs[0], result)
        return
    tracks = parameters.get("tracks")
    if not isinstance(tracks, list) or not 1 <= len(tracks) <= 16:
        raise ValueError("tracks must contain 1..16 audio streams")
    denoise = parameters.get("denoise", recipe == audio.RECIPE)
    levels = parameters.get("levels", True)
    if recipe == LEVELS_RECIPE and (denoise is not False or levels is not True):
        raise ValueError("audio-levels-v1 requires levels only")
    # Validate all paths and outputs before expensive processing.
    planned = []
    roles = set()
    for track in tracks:
        if not isinstance(track, dict) or set(track) != {"input", "stream_index", "role", "output"}:
            raise ValueError("tracks require input, stream_index, role, and output")
        input_path, output_path = local_path(root, track["input"]), local_path(root, track["output"])
        if input_path not in inputs:
            raise ValueError("audio input was not assigned")
        role = track["role"]
        if not isinstance(role, str) or not role or role in roles:
            raise ValueError("track roles must be nonempty and unique")
        roles.add(role)
        planned.append((track, input_path, output_path))
    reports = [path for path in outputs if path.suffix == ".json"]
    if len(reports) != 1 or set(outputs) != {reports[0], *(path for _, _, path in planned)} or len(outputs) != len(planned)+1:
        raise ValueError("audio outputs must be exactly the declared tracks and one JSON manifest")
    report = reports[0]
    enhanced = []
    for track, input_path, output_path in planned:
        result = audio.enhance(input_path, output_path, stream_index=track["stream_index"],
                               denoise=denoise, levels=levels,
                               attenuation_db=parameters.get("attenuation_db", 18.0),
                               scratch=scratch)
        enhanced.append({"file": os.path.relpath(output_path, report.parent), "role": track["role"],
                         "sha256": digest(output_path), "processing": result,
                         "source": {"path": track["input"], "stream_index": track["stream_index"],
                                    "sha256": digest(input_path)}})
    # A v1 capture manifest can feed the existing speech2md path directly.
    json_output(report, {"schemaVersion": 1, "meetingID": job["fingerprint"], "status": "complete",
                         "startedAt": None, "endedAt": None, "audio": enhanced,
                         "provenance": provenance})
