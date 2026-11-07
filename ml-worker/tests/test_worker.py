"""Exercise transport, recipe execution, and lease fencing without model downloads."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import types
import threading
import time
from urllib.parse import unquote, urlsplit
import wave

import numpy as np
import pytest

from ml_worker import audio, recipes, text, worker
from ml_worker.client import Client


def waveform(path, *, channels=1, amplitude=0.01):
    frames = 48000 + 1777
    samples = amplitude*np.sin(np.arange(frames)*2*np.pi*440/48000)
    samples = np.repeat(samples[:, None], channels, axis=1)
    with wave.open(str(path), "wb") as stream:
        stream.setparams((channels, 2, 48000, 0, "NONE", "not compressed"))
        stream.writeframes((samples*32767).astype('<i2').tobytes())
    return frames


@pytest.fixture
def server():
    state = {"inputs": {}, "outputs": {}, "completed": [], "failed": [], "assignment": None,
             "lose_lease": lambda: False, "corrupt": False, "completion_requests": 0}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def respond(self, value, status=200):
            data = json.dumps(value, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def authorized(self):
            if self.headers.get("Authorization") != "Bearer fixture-token":
                self.respond({"error": "unauthorized"}, 403)
                return False
            return True
        def do_GET(self):
            if not self.authorized(): return
            path = unquote(urlsplit(self.path).path).lstrip('/')
            data = state['inputs'][path]
            if state['corrupt']: data += b'corruption'
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_PUT(self):
            if not self.authorized(): return
            path = unquote(urlsplit(self.path).path).lstrip('/')
            assert path in state['assignment']['outputs']
            assert 'attempt=lease' in self.path
            data = self.rfile.read(int(self.headers['Content-Length']))
            state['outputs'][path] = data
            self.respond({'oid': hashlib.sha256(data).hexdigest(), 'size': len(data)})
        def do_POST(self):
            if not self.authorized(): return
            value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if value['op'] == 'claim':
                assert state['assignment']['job']['recipe'] in value['recipes']
                self.respond(state['assignment'])
                return
            assert value['id'] == 'fixture' and value['attempt'] == 'lease'
            if value['op'] == 'search':
                self.respond({'results': [{'metadata': {'identity': 'people/alice.md'}}]})
                return
            if value['op'] == 'complete':
                state['completion_requests'] += 1
                if value not in state['completed']:
                    state['completed'].append(value)
                # Commit succeeds but the response is lost; retry must use the same bytes.
                if state['completion_requests'] == 1:
                    self.close_connection = True
                    return
            if value.get('failure'): state['failed'].append(value['failure'])
            if value['op'] == 'heartbeat' and state['lose_lease']():
                self.respond({'error': 'expired'}, 409)
            else:
                self.respond(None)
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield Client(f'http://127.0.0.1:{http.server_port}', 'fixture-token'), state
    http.shutdown()
    http.server_close()
    thread.join()


def assignment(recipe, parameters, inputs, outputs):
    return {'job': {'id': 'fixture', 'source': 'jobs/source.md', 'recipe': recipe,
                    'attempt': 'lease', 'fingerprint': 'fingerprint', 'source_revision': 'revision'},
            'recording': '# Source\n', 'parameters': {'ml': parameters},
            'inputs': {path: {'oid': hashlib.sha256(value).hexdigest(), 'size': len(value)}
                       for path, value in inputs.items()}, 'outputs': outputs}


def fixture_models(monkeypatch, *, started=None):
    monkeypatch.setattr(worker, "capabilities", lambda: list(recipes.RECIPES))
    monkeypatch.setattr(text, "preload", lambda *args, **kwargs: None)
    def embed(p, **kwargs):
        text.records(p['texts'])
        if started is not None:
            started.set()
            # Wait until the HTTP heartbeat has rejected our lease.
            assert rejected.wait(5)
            time.sleep(0.1)  # Keep inference running while the response reaches the worker.
        return {'space': {'namespace': p.get('namespace', 'documents'), 'recipe': text.EMBED_RECIPE, 'dimensions': text.DIMENSIONS},
                'records': [{'id': x['id'], 'vector': [1.0]+[0.0]*(text.DIMENSIONS-1), 'metadata': {'text': x['text']}} for x in p['texts']]}
    def rerank(p, **kwargs):
        text.records(p['texts'])
        return {'results': [{'index': i, 'id': p['texts'][i]['id'], 'relevance_score': float(i)} for i in reversed(range(len(p['texts'])))]}
    rejected = threading.Event()
    monkeypatch.setattr(text, 'embeddings', embed)
    monkeypatch.setattr(text, 'rerank', rerank)
    return rejected


def fixture_speech(monkeypatch):
    def transcribe(source, *, force):
        assert force
        moss = source.with_suffix('.transcript.moss.npz')
        if 'Alice' in source.read_text():
            assert moss.read_bytes() == b'raw MOSS generation'
        else:
            assert not moss.exists()
        moss.write_bytes(b'raw MOSS generation')
        source.with_suffix('.transcript.md').write_text('# Transcript')
        source.with_suffix('.transcript.voiceprints.json').write_text(json.dumps({
            'space': {}, 'records': [{'id': 'speaker', 'vector': [1.0], 'metadata': {}}]}))
    monkeypatch.setitem(sys.modules, 'speech2md', types.ModuleType('speech2md'))
    monkeypatch.setitem(sys.modules, 'speech2md.pipeline', types.SimpleNamespace(transcribe=transcribe))


@pytest.mark.parametrize(('recipe', 'capture_version', 'retrieval'), [
    (text.EMBED_RECIPE, None, False), (text.RERANK_RECIPE, None, False), (recipes.LEVELS_RECIPE, None, False),
    (text.EMBED_RECIPE, None, True), (text.RERANK_RECIPE, None, True),
    *((recipes.SPEECH_RECIPE, version, False) for version in (None, 1, 2, 3)),
])
def test_claim_process_and_publish(server, tmp_path, monkeypatch, recipe, capture_version, retrieval):
    client, state = server
    fixture_models(monkeypatch)
    monkeypatch.setenv('MDSTORE_WORKER_TOKEN', 'fixture-token')
    if recipe == recipes.SPEECH_RECIPE:
        fixture_speech(monkeypatch)
        state['inputs'] = {'jobs/input.wav': b'fixture'}
        params = {}
        outputs = ['jobs/source.transcript.md','jobs/source.transcript.voiceprints.json']
    elif recipe == recipes.LEVELS_RECIPE:
        source = tmp_path/'original.wav'
        waveform(source, channels=2)
        state['inputs'] = {'audio/original.wav': source.read_bytes()}
        params = {'tracks': [{'input': 'audio/original.wav', 'stream_index': 0,
                              'role': 'participants', 'output': 'audio/enhanced.wav'}]}
        outputs = ['audio/enhanced.wav', 'audio/enhanced-capture.json']
    else:
        params = {'texts': [{'id': 'a', 'text': 'one'}, {'id': 'b', 'text': 'two'}]}
        if recipe == text.RERANK_RECIPE: params['query'] = 'two'
        outputs = ['jobs/result.json']
    state['assignment'] = assignment(recipe, params, state['inputs'], outputs)
    if recipe == recipes.SPEECH_RECIPE:
        if capture_version is None:
            state['assignment']['recording'] = '---\naudio: input.wav\n---\n# Recording\n'
        else:
            capture = {'schemaVersion': capture_version, 'meetingID': 'fixture', 'status': 'complete',
                       'startedAt': None, 'endedAt': None}
            checksum = hashlib.sha256(state['inputs']['jobs/input.wav']).hexdigest()
            if capture_version == 1:
                capture['audio'] = [{'file': 'input.wav', 'role': 'mixed', 'sha256': checksum}]
            else:
                capture['container'] = {'file': 'input.wav', 'sha256': checksum}
                capture['audio'] = [{'role': 'process' if capture_version == 3 else 'participants',
                                     'id': 'zoom', 'process': {'name': 'Zoom'}, 'streamIndex': 0}]
            state['inputs']['jobs/capture.json'] = json.dumps(capture).encode()
            state['assignment'] = assignment(recipe, params, state['inputs'], outputs)
            state['assignment']['recording'] = '---\nmanifest: capture.json\n---\n# Recording\n'
    state['assignment']['retrieval'] = retrieval
    assert worker.main(['--server', client.server, '--once', '--recipes', recipe,
                        '--cache', str(tmp_path/'cache')]) == 0
    assert len(state['completed']) == 1 and not state['failed']
    assert state['completion_requests'] == 2
    if retrieval:
        assert not list((tmp_path/'cache').rglob('work'))
        assert not list((tmp_path/'cache').rglob('.worker.lock'))
    complete = state['completed'][0]
    assert set(complete['assets']) == {p for p in outputs if not p.endswith('.md') and not retrieval}
    assert set(complete['outputs']) == {p for p in outputs if p.endswith('.md') or retrieval}
    for path, asset in complete['assets'].items():
        assert asset['oid'] == hashlib.sha256(state['outputs'][path]).hexdigest()
    result = json.loads(complete['outputs'][outputs[-1]] if retrieval else state['outputs'][outputs[-1]])
    if recipe == recipes.SPEECH_RECIPE:
        assert result['records'][0]['metadata']['suggestions'][0]['metadata']['identity'] == 'people/alice.md'
        previous = next((tmp_path/'cache').rglob('source.transcript.md')).parent.parent
        # A speaker correction changes the job fingerprint, but not its raw inference cache.
        state['assignment']['job']['fingerprint'] = 'speaker-correction'
        state['assignment']['recording'] = state['assignment']['recording'].replace('\n---\n', '\nattendees: [{handle: Alice, identity: ""}]\n---\n')
        state['completed'].clear()
        state['completion_requests'] = 0
        monkeypatch.setenv('MDSTORE_WORKER_TOKEN', 'fixture-token')
        assert worker.main(['--server', client.server, '--once', '--recipes', recipe,
                            '--cache', str(tmp_path/'cache')]) == 0
        assert len(state['completed']) == 1 and not state['failed']
        assert state['completion_requests'] == 2
        assert not previous.exists()
        assert len(list((tmp_path/'cache').rglob('input.wav'))) == 1
        return
    assert result['provenance']['fingerprint'] == 'fingerprint'
    if recipe == recipes.LEVELS_RECIPE:
        enhanced = tmp_path/'enhanced.wav'
        enhanced.write_bytes(state['outputs']['audio/enhanced.wav'])
        assert audio.measure(enhanced)['frames'] == audio.measure(source)['frames']
        assert audio.measure(enhanced)['channels'] == 2
        assert result['audio'][0]['processing']['gain_db'] == 12
    elif recipe == text.RERANK_RECIPE:
        assert result['results'][0]['id'] == 'b'
    else:
        assert result['space']['namespace'] == 'documents'
        assert len(result['records'][0]['vector']) == text.DIMENSIONS


@pytest.mark.parametrize('retrieval', [False, True])
def test_failed_lease_finishes_inference_and_never_publishes(server, tmp_path, monkeypatch, retrieval):
    client, state = server
    started = threading.Event()
    rejected = fixture_models(monkeypatch, started=started)
    def lose_lease():
        if started.is_set():
            rejected.set()
            return True
        return False
    state['assignment'] = assignment(text.EMBED_RECIPE, {'texts': [{'id':'x','text':'hello'}]}, {}, ['jobs/result.json'])
    state['assignment']['retrieval'] = retrieval
    state['lose_lease'] = lose_lease
    models = worker.Models()
    try:
        with pytest.raises(RuntimeError, match='lease lost'):
            worker.execute(client, state['assignment'], tmp_path/'cache', heartbeat_seconds=0.05, models=models)
        assert started.is_set() and rejected.is_set()
        assert not state['outputs'] and not state['completed']
        if retrieval:
            assert not list((tmp_path/'cache').rglob('work'))
        state['lose_lease'] = lambda: False
        worker.execute(client, state['assignment'], tmp_path/'cache', models=models)
        assert len(state['completed']) == 1
    finally:
        models.close()


def test_corrupt_input_and_unassigned_audio_never_publish(server, tmp_path):
    client, state = server
    state['inputs'] = {'audio/input.wav': b'fixture'}
    params = {'tracks': [{'input':'audio/input.wav','output':'audio/output.wav','role':'microphone','stream_index':0}]}
    state['assignment'] = assignment(recipes.LEVELS_RECIPE, params, state['inputs'], ['audio/output.wav','audio/capture.json'])
    state['corrupt'] = True
    with pytest.raises(ValueError, match='assigned size'):
        worker.execute(client, state['assignment'], tmp_path/'cache', models=None)
    assert state['failed'] and not state['completed']
    state['assignment']['inputs'] = {}
    with pytest.raises(ValueError, match='not assigned'):
        recipes.execute(state['assignment'], tmp_path)


def test_audio_enhancement_keeps_tail_channels_and_silence(tmp_path):
    if not __import__('shutil').which('deep-filter-worker'):
        pytest.skip('requires the native DeepFilterNet3 helper')
    source, output = tmp_path/'source.wav', tmp_path/'enhanced.wav'
    count = waveform(source, channels=2)
    result = audio.enhance(source, output)
    assert audio.measure(output)['frames'] == count
    assert audio.measure(output)['channels'] == 2
    assert result['model'] == 'DeepFilterNet3'
    assert result['gain_db'] <= 12
    first = output.read_bytes()
    denoiser = audio._denoiser
    audio.enhance(source, output)
    assert output.read_bytes() == first
    assert audio._denoiser is denoiser and denoiser.poll() is None
    waveform(source, amplitude=0)
    audio.enhance(source, output)
    assert audio.measure(output)['rms_dbfs'] is None
    models = worker.Models(idle_seconds=0)
    try:
        with models.use(audio.RECIPE):
            models.evict()  # An active model must survive maintenance.
            assert audio._denoiser is denoiser
        models.evict()
        assert audio._denoiser is None and denoiser.poll() is not None
    finally:
        models.close()
    audio.enhance(source, output, denoise=False)
    assert audio.measure(output)['rms_dbfs'] is None
