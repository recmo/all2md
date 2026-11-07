"""Delay-compensated DeepFilterNet3 and bounded level adjustment, per stream."""
from __future__ import annotations

import math
import json
from pathlib import Path
import subprocess
import tempfile
import wave

import numpy as np

RECIPE = "audio-enhancement-dfn3-v1"
MODEL = "DeepFilterNet3"
# The Nix build embeds this release's model, rather than downloading mutable weights.
MODEL_REVISION = "DeepFilterNet-v0.5.6"
SAMPLE_RATE = 48000


# Native DFN runtime, shared across tracks and jobs.
_denoiser = None


def close_denoiser():
    global _denoiser
    if _denoiser is not None:
        if _denoiser.poll() is None:
            _denoiser.kill()
        _denoiser.wait()
        _denoiser.stdin.close()
        _denoiser.stdout.close()
        _denoiser = None


def denoise_file(source, output, attenuation_db):
    global _denoiser
    if _denoiser is not None and _denoiser.poll() is not None:
        close_denoiser()
    if _denoiser is None:
        _denoiser = subprocess.Popen(["deep-filter-worker"], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, text=True)
    try:
        _denoiser.stdin.write(json.dumps({"source": str(source), "output": str(output),
                                        "attenuation_db": attenuation_db}) + "\n")
        _denoiser.stdin.flush()
        response = _denoiser.stdout.readline()
        if not response:
            raise RuntimeError("DeepFilterNet helper exited")
        error = json.loads(response)
    except BaseException:
        close_denoiser()
        raise
    if error is not None:
        raise RuntimeError(error)


def run(*args):
    subprocess.run(args, check=True, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=None)


def measure(path, frames=None):
    peak, power, count = 0, 0.0, 0
    with wave.open(str(path), "rb") as audio:
        if audio.getsampwidth() != 2 or audio.getframerate() != SAMPLE_RATE:
            raise ValueError("enhancement requires 48 kHz PCM16")
        available, channels = audio.getnframes(), audio.getnchannels()
        if channels not in {1, 2} or available <= 0:
            raise ValueError("enhancement requires nonempty mono or stereo audio")
        frames = available if frames is None else frames
        if available < frames:
            raise ValueError("denoising shortened the source after delay compensation")
        remaining = frames
        while remaining:
            data = audio.readframes(min(remaining, SAMPLE_RATE))
            samples = np.frombuffer(data, dtype="<i2").astype(np.float64) / 32768
            if not samples.size:
                raise ValueError("truncated waveform")
            peak = max(peak, float(np.max(np.abs(samples))))
            power += float(np.dot(samples, samples))
            count += samples.size
            remaining -= samples.size // channels
    return {"frames": frames, "channels": channels, "sample_rate": SAMPLE_RATE,
            "peak_dbfs": 20*math.log10(peak) if peak else None,
            "rms_dbfs": 10*math.log10(power/count) if power else None}


def level_gain(stats, *, target_dbfs=-23.0, max_gain_db=12.0):
    rms, peak = stats["rms_dbfs"], stats["peak_dbfs"]
    if rms is None or rms < -60 or peak is None:
        return 0.0
    return min(target_dbfs-rms, max_gain_db, -1.0-peak)


def enhance(source: Path, output: Path, *, stream_index=0, denoise=True, levels=True,
            attenuation_db=18.0, scratch=None):
    if type(stream_index) is not int or stream_index < 0:
        raise ValueError("stream_index must be a nonnegative integer")
    if type(denoise) is not bool or type(levels) is not bool or not (denoise or levels):
        raise ValueError("enable denoise, levels, or both")
    if isinstance(attenuation_db, bool) or not isinstance(attenuation_db, (int, float)) or not 0 <= attenuation_db <= 36:
        raise ValueError("attenuation_db must be between 0 and 36")
    if output.suffix.lower() not in {".wav", ".flac", ".m4a"}:
        raise ValueError("enhanced output must be .wav, .flac, or .m4a")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".enhance-", dir=scratch or output.parent) as temporary:
        root = Path(temporary)
        decoded = root / "source.wav"
        run("ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source),
            "-map", f"0:a:{stream_index}", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(decoded))
        original = measure(decoded)
        processed = decoded
        if denoise:
            padded = root / "padded.wav"
            # DFN trims its lookahead but does not flush the tail. Padding
            # before inference protects the final speech; trim it only afterwards.
            run("ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(decoded),
                "-af", "apad=pad_dur=1", "-c:a", "pcm_s16le", str(padded))
            processed = root / "denoised.wav"
            denoise_file(padded, processed, attenuation_db)
        stats = measure(processed, original["frames"])
        gain = level_gain(stats) if levels else 0.0
        codec = {".wav": ["-c:a", "pcm_s16le"], ".flac": ["-c:a", "flac"],
                 ".m4a": ["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"]}[output.suffix.lower()]
        staged = root / output.name
        run("ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(processed),
            "-af", f"atrim=end_sample={original['frames']},volume={gain:.9f}dB",
            *codec, str(staged))
        staged.replace(output)
    return {"recipe": RECIPE, "model": MODEL if denoise else None,
            "model_revision": MODEL_REVISION if denoise else None,
            "denoise": denoise, "levels": levels, "attenuation_db": attenuation_db if denoise else None,
            "gain_db": gain, "input": original, "processed": stats,
            "target_rms_dbfs": -23, "max_gain_db": 12, "peak_ceiling_dbfs": -1}
