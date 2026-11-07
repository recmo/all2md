"""Exercise real transport, recipe subprocesses, and cancellation without model downloads."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.parse import unquote, urlsplit
import wave

import numpy as np
import pytest

from ml_worker import audio, inference as inference_module, recipes, text, worker
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


def fixture_models(monkeypatch, *, started=None, loads=None):
    monkeypatch.setattr(worker, "capabilities", lambda: list(recipes.RECIPES))
    original = subprocess.Popen
    processes = []
    def popen(command, **kwargs):
        if '--inference-fd' in command and set(command) & {text.EMBED_RECIPE, text.RERANK_RECIPE}:
            code = '''
import os, sys, runpy
from ml_worker import text
assert 'MDSTORE_WORKER_TOKEN' not in os.environ
assert 'CUSTOM_TOKEN' not in os.environ
from types import SimpleNamespace
class Factory:
    @classmethod
    def from_pretrained(cls, model, **kwargs):
        assert kwargs['trust_remote_code'] is False
        if loads is not None and 'dtype' in kwargs:
            with open(loads, 'a') as stream:
                stream.write(model + '\\n')
        return cls()
    def to(self, device): return self
    def eval(self): return self
sys.modules['torch'] = SimpleNamespace(float32='fp32', bfloat16='bf16',
    backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: False)))
sys.modules['transformers'] = SimpleNamespace(AutoModel=Factory, AutoModelForCausalLM=Factory, AutoTokenizer=Factory)

def embed(p, **kwargs):
    text.records(p['texts'])
    if started:
        from pathlib import Path
        import time
        if not Path(started).exists():
            Path(started).touch()
            time.sleep(10)
    text._load(text.EMBED_MODEL, text.EMBED_REVISION, classifier=False, device=kwargs['device'])
    return {'space': {'namespace': p.get('namespace', 'documents'), 'recipe': text.EMBED_RECIPE, 'dimensions': text.DIMENSIONS},
            'records': [{'id': x['id'], 'vector': [1.0]+[0.0]*(text.DIMENSIONS-1), 'metadata': {'text': x['text']}} for x in p['texts']]}
def rerank(p, **kwargs):
    text.records(p['texts'])
    text._load(text.RERANK_MODEL, text.RERANK_REVISION, classifier=True, device=kwargs['device'])
    return {'results': [{'index': i, 'id': p['texts'][i]['id'], 'relevance_score': float(i)} for i in reversed(range(len(p['texts'])))]}
text.embeddings, text.rerank = embed, rerank
'''
            code = f'loads = {str(loads)!r}\n' + code if loads is not None else 'loads = None\n' + code
            code = f'started = {str(started)!r}\n' + code if started is not None else 'started = None\n' + code
            code += f"sys.argv = {['ml_worker.worker', *command[command.index('--inference-fd'):]]!r}\nrunpy.run_module('ml_worker.worker', run_name='__main__')\n"
            command = [sys.executable, '-c', code]
        process = original(command, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(inference_module.subprocess, 'Popen', popen)
    return processes


def fixture_speech(tmp_path, monkeypatch):
    # Use production model-cache functions, with a tiny fake MLX loader and
    # pipeline so the lifecycle check runs on Linux without model weights.
    modules = tmp_path/'modules'
    speech = modules/'speech2md'
    speech.mkdir(parents=True)
    production = Path(os.environ.get('SPEECH2MD_SOURCE',
        str(Path(__file__).resolve().parents[2]/'speech2md/src/speech2md')))
    (speech/'__init__.py').write_text(f'__path__.append({str(production)!r})\n')
    (speech/'pipeline.py').write_text('''from .moss import load_moss_engine
from .redimnet2 import get_redimnet2_embedder
import json, os

def transcribe(source, *, force):
    assert force
    assert 'MDSTORE_WORKER_TOKEN' not in os.environ
    moss = source.with_suffix('.transcript.moss.npz')
    if 'Alice' in source.read_text():
        assert moss.read_bytes() == b'raw MOSS generation'
    else:
        load_moss_engine()
    moss.write_bytes(b'raw MOSS generation')
    assert get_redimnet2_embedder() is get_redimnet2_embedder()
    source.with_suffix('.transcript.md').write_text('# Transcript')
    source.with_suffix('.transcript.voiceprints.json').write_text(json.dumps({'space': {}, 'records': [{'id': 'speaker', 'vector': [1.0], 'metadata': {}}]}))
''')
    mlx = modules/'mlx_audio'
    mlx.mkdir()
    (mlx/'__init__.py').write_text('')
    (mlx/'stt.py').write_text(f'''def load(*args, **kwargs):
    with open({str(tmp_path/'loads')!r}, 'a') as stream:
        stream.write('loaded\\n')
    return object()
''')
    monkeypatch.setenv('PYTHONPATH', str(modules)+os.pathsep+os.environ.get('PYTHONPATH', ''))


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
        fixture_speech(tmp_path, monkeypatch)
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
        assert not list((tmp_path/'cache').rglob('.assignment.json'))
        assert not list((tmp_path/'cache').rglob('.worker.lock'))
    complete = state['completed'][0]
    assert set(complete['assets']) == {p for p in outputs if not p.endswith('.md') and not retrieval}
    assert set(complete['outputs']) == {p for p in outputs if p.endswith('.md') or retrieval}
    for path, asset in complete['assets'].items():
        assert asset['oid'] == hashlib.sha256(state['outputs'][path]).hexdigest()
    result = json.loads(complete['outputs'][outputs[-1]] if retrieval else state['outputs'][outputs[-1]])
    if recipe == recipes.SPEECH_RECIPE:
        assert result['records'][0]['metadata']['suggestions'][0]['metadata']['identity'] == 'people/alice.md'
        previous = next((tmp_path/'cache').rglob('.assignment.json')).parent
        # A speaker correction changes the job fingerprint, but not its raw inference cache.
        state['assignment']['job']['fingerprint'] = 'speaker-correction'
        state['assignment']['recording'] = state['assignment']['recording'].replace('\n---\n', '\nattendees: [{handle: Alice, identity: ""}]\n---\n')
        state['completed'].clear()
        state['completion_requests'] = 0
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
def test_failed_lease_kills_recipe_and_never_publishes(server, tmp_path, monkeypatch, retrieval):
    client, state = server
    marker = tmp_path/'started'
    processes = fixture_models(monkeypatch, started=marker)
    state['assignment'] = assignment(text.EMBED_RECIPE, {'texts': [{'id':'x','text':'hello'}]}, {}, ['jobs/result.json'])
    state['assignment']['retrieval'] = retrieval
    state['lose_lease'] = marker.exists
    inference = worker.Inference([text.EMBED_RECIPE, text.RERANK_RECIPE], 'auto', 'MDSTORE_WORKER_TOKEN')
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeError, match='lease lost'):
            worker.execute(client, state['assignment'], tmp_path/'cache', heartbeat_seconds=0.05, inference=inference)
        assert time.monotonic()-started < 5
        assert processes and all(p.poll() is not None for p in processes)
        assert not state['outputs'] and not state['completed']
        if retrieval:
            assert not list((tmp_path/'cache').rglob('.assignment.json'))
            assert not list((tmp_path/'cache').rglob('.worker.lock'))
        state['lose_lease'] = lambda: False
        worker.execute(client, state['assignment'], tmp_path/'cache', inference=inference)
        assert len(processes) == 2 and len(state['completed']) == 1
        assert inference.process.poll() is None
    finally:
        inference.close()


def test_corrupt_input_and_unassigned_audio_never_publish(server, tmp_path):
    client, state = server
    state['inputs'] = {'audio/input.wav': b'fixture'}
    params = {'tracks': [{'input':'audio/input.wav','output':'audio/output.wav','role':'microphone','stream_index':0}]}
    state['assignment'] = assignment(recipes.LEVELS_RECIPE, params, state['inputs'], ['audio/output.wav','audio/capture.json'])
    state['corrupt'] = True
    with pytest.raises(ValueError, match='assigned size'):
        worker.execute(client, state['assignment'], tmp_path/'cache')
    assert state['failed'] and not state['completed']
    state['assignment']['inputs'] = {}
    with pytest.raises(ValueError, match='not assigned'):
        recipes.execute(state['assignment'], tmp_path)


def test_audio_enhancement_keeps_tail_channels_and_silence(tmp_path):
    if not __import__('shutil').which('deep-filter-worker'):
        pytest.skip('requires the Nix DeepFilterNet3 CLI')
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
    audio.close_denoiser()
    audio.enhance(source, output, denoise=False)
    assert audio.measure(output)['rms_dbfs'] is None


def test_text_models_stay_hot_across_jobs_and_recipe_errors(server, tmp_path, monkeypatch):
    client, state = server
    loads = tmp_path/'loads'
    processes = fixture_models(monkeypatch, loads=loads)
    selected = [text.EMBED_RECIPE, text.RERANK_RECIPE]
    inference = worker.Inference(selected, 'cpu', 'CUSTOM_TOKEN')
    monkeypatch.setenv('MDSTORE_WORKER_TOKEN', 'fixture-token')
    monkeypatch.setenv('CUSTOM_TOKEN', 'also-secret')
    try:
        inference.start(threading.Event())
        assert loads.read_text().splitlines() == [text.EMBED_MODEL, text.RERANK_MODEL]
        process = inference.process
        for i, recipe in enumerate([*selected, text.EMBED_RECIPE, text.RERANK_RECIPE]):
            params = {'texts': [{'id': 'x', 'text': 'hello'}]}
            if recipe == text.RERANK_RECIPE:
                params['query'] = 'hello'
            state['assignment'] = assignment(recipe, params, {}, ['jobs/result.json'])
            state['assignment']['retrieval'] = bool(i % 2)
            state['assignment']['job']['fingerprint'] = str(i)
            if i == 2:
                params['texts'] = []
                with pytest.raises(RuntimeError, match='texts must contain'):
                    worker.execute(client, state['assignment'], tmp_path/'cache', device='cpu', inference=inference)
                params['texts'] = [{'id': 'x', 'text': 'hello'}]
            worker.execute(client, state['assignment'], tmp_path/'cache', device='cpu', inference=inference)
            assert inference.process is process and process.poll() is None
        assert len(processes) == 1
        assert loads.read_text().splitlines() == [text.EMBED_MODEL, text.RERANK_MODEL]
    finally:
        inference.close()
    assert process.poll() is not None


@pytest.mark.parametrize('recipe', [audio.RECIPE, recipes.SPEECH_RECIPE])
def test_audio_and_speech_backends_reuse_evict_and_reload(server, tmp_path, monkeypatch, recipe):
    client, state = server
    monkeypatch.setenv('MDSTORE_WORKER_TOKEN', 'fixture-token')
    source = tmp_path/'source.wav'
    waveform(source)
    state['inputs'] = {'jobs/input.wav': source.read_bytes()}
    if recipe == recipes.SPEECH_RECIPE:
        fixture_speech(tmp_path, monkeypatch)
        outputs = ['jobs/source.transcript.md', 'jobs/source.transcript.voiceprints.json']
        params = {}
    else:
        outputs = ['jobs/enhanced.wav', 'jobs/capture.json']
        params = {'tracks': [{'input': 'jobs/input.wav', 'output': outputs[0],
                              'stream_index': 0, 'role': 'microphone'}]}
    state['assignment'] = assignment(recipe, params, state['inputs'], outputs)
    if recipe == recipes.SPEECH_RECIPE:
        state['assignment']['recording'] = '---\naudio: input.wav\n---\n# Recording\n'
    backend = worker.Inference([recipe], 'cpu', 'MDSTORE_WORKER_TOKEN', idle_seconds=1.0)
    try:
        assert backend.process is None  # Lazy until the first job.
        worker.execute(client, state['assignment'], tmp_path/'cache', inference=backend)
        process = backend.process
        worker.execute(client, state['assignment'], tmp_path/'cache', inference=backend)
        assert backend.process is process and process.poll() is None
        if recipe == recipes.SPEECH_RECIPE:
            assert (tmp_path/'loads').read_text().splitlines() == ['loaded']
        deadline = time.monotonic() + 5
        while backend.connection is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert process.poll() is not None
        assert backend.process is None and backend.connection is None
        worker.execute(client, state['assignment'], tmp_path/'cache', inference=backend)
        assert backend.process is not process and backend.process.poll() is None
        if recipe == recipes.SPEECH_RECIPE:
            assert (tmp_path/'loads').read_text().splitlines() == ['loaded', 'loaded']
    finally:
        backend.close()
