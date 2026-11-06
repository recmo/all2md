from pathlib import Path
import pytest

from speech2md.recording import frontmatter, resolve_recording
from speech2md.hints import load_hints


def test_recording_projection_ignores_notes_and_yaml_formatting(tmp_path):
    path = tmp_path / 'recording.md'
    path.write_text('---\naudio: audio.wav\nhotwords: [ProveKit]\n---\n# Meeting\n')
    first = load_hints(path).sha256
    path.write_text('---\nhotwords:\n  - ProveKit\naudio: audio.wav\n---\n# Changed notes\n')
    assert load_hints(path).sha256 == first
    resolved = resolve_recording(path, frontmatter(path))
    assert resolved.markdown_path == tmp_path / 'transcript.md'
    assert resolved.sources[0][0] == tmp_path / 'audio.wav'
    path.write_text('---\naudio: ../escape.wav\n---\n')
    with pytest.raises(ValueError, match='inside'):
        resolve_recording(path, frontmatter(path))
