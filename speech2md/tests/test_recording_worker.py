from pathlib import Path
import json
import pytest

from speech2md.recording import frontmatter, resolve_recording
from speech2md.hints import load_hints
from speech2md.worker import local_path, execute


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
    with pytest.raises(ValueError):
        local_path(tmp_path, '../escape')


def test_worker_downloads_inputs_and_publishes_assigned_outputs(tmp_path, monkeypatch):
    calls = []
    class Client:
        server = 'http://fixture'
        def download(self, query, target, asset):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'audio')
        def request(self, path, value=None, **kwargs):
            calls.append((path, value, kwargs))
            return {'oid':'a'*64, 'size': 1}
    class Process:
        returncode = 0
        def __init__(self, command, **kwargs):
            recording = Path(command[-2])
            recording.with_name('transcript.md').write_text('# Derived\n')
            recording.with_name('transcript.voiceprints.json').write_text(json.dumps({'space': {}, 'records': []}))
        def poll(self): return 0
    monkeypatch.setattr('speech2md.worker.subprocess.Popen', Process)
    execute(Client(), {
        'job': {'id': 'fixture', 'source': 'meeting/recording.md', 'attempt': 'lease'},
        'recording': '---\naudio: audio.wav\n---\n# Meeting\n',
        'inputs': {'meeting/audio.wav': {'oid': 'a'*64, 'size': 5}},
        'outputs': ['meeting/transcript.md', 'meeting/transcript.voiceprints.json'],
    }, tmp_path)
    assert calls[-1][0].endswith('/complete')
    assert calls[-1][1]['outputs'] == {'meeting/transcript.md': '# Derived\n'}
    assert calls[-1][1]['assets']['meeting/transcript.voiceprints.json']['oid'] == 'a'*64


def test_completion_retries_identical_bytes_after_lost_response(monkeypatch):
    import io
    from urllib.error import URLError
    from speech2md import worker

    requests = []

    def urlopen(request, **kwargs):
        requests.append(request)
        if len(requests) == 1:
            raise URLError("response lost")
        return io.BytesIO(b"null")

    monkeypatch.setattr(worker, "urlopen", urlopen)
    client = worker.Client("http://localhost", "worker-token")
    assert client.request("/mcp/worker/jobs/job/complete", {"attempt": "lease", "outputs": {}}) is None
    assert len(requests) == 2
    assert requests[0].data == requests[1].data


def test_worker_does_not_retry_non_idempotent_claim(monkeypatch):
    from urllib.error import URLError
    from speech2md import worker
    import pytest

    calls = []

    def urlopen(request, **kwargs):
        calls.append(request)
        raise URLError("connection lost")

    monkeypatch.setattr(worker, "urlopen", urlopen)
    with pytest.raises(URLError):
        worker.Client("http://localhost", "worker-token").request("/mcp/worker/claim", {"recipes": []})
    assert len(calls) == 1
