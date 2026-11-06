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
    original = subprocess.Popen
    processes = []
    def popen(command, **kwargs):
        if command[1:3] == ['-m', 'ml_worker.worker']:
            code = '''
import os, sys, runpy
from ml_worker import text
assert 'MDSTORE_WORKER_TOKEN' not in os.environ

def embed(p, **kwargs):
    text.records(p['texts'])
    return {'space': {'namespace': p.get('namespace', 'documents'), 'recipe': text.EMBED_RECIPE, 'dimensions': text.DIMENSIONS},
            'records': [{'id': x['id'], 'vector': [1.0]+[0.0]*(text.DIMENSIONS-1), 'metadata': {'text': x['text']}} for x in p['texts']]}
def rerank(p, **kwargs):
    text.records(p['texts'])
    return {'results': [{'index': i, 'id': p['texts'][i]['id'], 'relevance_score': float(i)} for i in reversed(range(len(p['texts'])))]}
text.embeddings, text.rerank = embed, rerank
'''
            if started:
                code += f"from pathlib import Path\nPath({str(started)!r}).touch()\nimport time\ntime.sleep(10)\n"
            code += f"sys.argv = {['ml_worker.worker', *command[3:]]!r}\nrunpy.run_module('ml_worker.worker', run_name='__main__')\n"
            command = [sys.executable, '-c', code]
        process = original(command, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(worker.subprocess, 'Popen', popen)
    return processes


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
        cli = tmp_path/'speech2md'
        cli.write_text(f'#!{sys.executable}\n' + '''from pathlib import Path
import json, sys
recording=Path(sys.argv[1])
assert sys.argv[2] == '--force'
moss = recording.with_suffix('.transcript.moss.npz')
if 'Alice' in recording.read_text():
    assert moss.read_bytes() == b'raw MOSS generation'
else:
    assert not moss.exists()
moss.write_bytes(b'raw MOSS generation')
recording.with_suffix('.transcript.md').write_text('# Transcript\\n')
recording.with_suffix('.transcript.voiceprints.json').write_text(json.dumps({'space':{}, 'records':[{'id':'speaker','vector':[1.0], 'metadata':{}}]}))
''')
        cli.chmod(0o755)
        monkeypatch.setenv('PATH', str(tmp_path)+os.pathsep+os.environ['PATH'])
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
    started = time.monotonic()
    with pytest.raises(RuntimeError, match='lease lost'):
        worker.execute(client, state['assignment'], tmp_path/'cache', heartbeat_seconds=0.05)
    assert time.monotonic()-started < 5
    assert processes and all(p.poll() is not None for p in processes)
    assert not state['outputs'] and not state['completed']
    if retrieval:
        assert not list((tmp_path/'cache').rglob('.assignment.json'))
        assert not list((tmp_path/'cache').rglob('.worker.lock'))


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
    if not __import__('shutil').which('deep-filter'):
        pytest.skip('requires the Nix DeepFilterNet3 CLI')
    source, output = tmp_path/'source.wav', tmp_path/'enhanced.wav'
    count = waveform(source, channels=2)
    result = audio.enhance(source, output)
    assert audio.measure(output)['frames'] == count
    assert audio.measure(output)['channels'] == 2
    assert result['model'] == 'DeepFilterNet3'
    assert result['gain_db'] <= 12
    waveform(source, amplitude=0)
    audio.enhance(source, output, denoise=False)
    assert audio.measure(output)['rms_dbfs'] is None
