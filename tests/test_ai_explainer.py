import copy
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from emsolver.ai_explainer import LocalModelConfig, explain_diagnosis
from emsolver.knowledge import diagnose


def model(**changes):
    return LocalModelConfig(**{'endpoint': 'http://127.0.0.1:1234/v1', 'model': 'local-example',
                              'model_version': 'sha256:reviewed', 'license_id': 'Apache-2.0',
                              'license_approved': True, **changes})


def response(value):
    return json.dumps({'choices': [{'message': {'content': json.dumps(value)}}]}).encode()


def test_missing_model_falls_back_to_the_actual_rules():
    rules = diagnose({'error': {'code': 'WALLTIME_EXCEEDED'}})
    explained = explain_diagnosis(rules)
    assert explained['suggestions'] == rules['suggestions']
    assert explained['explanation']['source'] == 'rules'
    assert explained['explanation']['fallback_reason'] == 'not_configured'
    assert rules.get('explanation') is None


def test_model_only_gets_allowlisted_aggregate_evidence_and_cannot_overwrite_rules():
    rules = diagnose({'error': {'code': 'SINGULAR_SHIFT'}})
    original = copy.deepcopy(rules)
    captured = []
    def transport(config, body):
        captured.append(json.loads(body))
        return response({'summary': '工作位移恢复需要重新验证。',
                         'items': [{'rule_id': 'near_singular', 'text': '目标中心保持冻结。'}]})
    result = explain_diagnosis(rules, {'_path': '/Users/private/data.npz', 'matrix': [[998877]],
                                     'result': {'eigenvalues': [998877]}, 'error': {'message': 'secret'}},
                              model(), transport=transport)
    sent = json.dumps(captured, ensure_ascii=False)
    assert '/Users/' not in sent and '998877' not in sent and 'secret' not in sent
    assert result['rules'] == original['rules'] and result['suggestions'] == original['suggestions']
    assert result['explanation']['source'] == 'local_model'
    assert result['explanation']['model']['license_id'] == 'Apache-2.0'
    assert result['explanation']['template_version']


@pytest.mark.parametrize('bad', [b'invalid json', response({'summary': 'x', 'items': [], 'status': 'SUCCEEDED'}),
                               response({'summary': '```python\nexec("bad")\n```', 'items': []}),
                               response({'summary': 'x', 'items': [{'rule_id': 'invented', 'text': 'x'}]}),
                               b' ' * 65537])
def test_invalid_or_executable_output_falls_back(bad):
    rules = diagnose({'error': {'code': 'SINGULAR_SHIFT'}})
    result = explain_diagnosis(rules, model_config=model(), transport=lambda *_: bad)
    assert result['explanation']['source'] == 'rules'
    assert result['explanation']['fallback_reason'] == 'invalid_response'
    assert result['suggestions'] == rules['suggestions']


def test_timeout_and_licence_not_approved_use_rules_without_download():
    def timeout(*_):
        raise socket.timeout()
    rules = diagnose(config={})
    assert explain_diagnosis(rules, model_config=model(), transport=timeout)['explanation']['fallback_reason'] == 'timeout'
    def never(*_):
        raise AssertionError('unapproved model must never be contacted')
    result = explain_diagnosis(rules, model_config=model(license_approved=False), transport=never)
    assert result['explanation']['fallback_reason'] == 'license_not_approved'


@pytest.mark.parametrize('endpoint', ['https://external.example/v1', 'http://192.168.1.1/v1',
                                      'http://127.0.0.1.evil.example/v1', 'http://user:pass@127.0.0.1/v1',
                                      'http://127.0.0.1/v1?token=x', 'file:///tmp/model'])
def test_non_loopback_or_credential_endpoints_are_rejected(endpoint):
    with pytest.raises(ValueError):
        model(endpoint=endpoint)


def test_model_timeout_and_response_limits_are_bounded():
    for changes in ({'timeout_seconds': 20}, {'max_response_bytes': 10**9}, {'model': ''}):
        with pytest.raises(ValueError):
            model(**changes)


def test_real_loopback_protocol_is_bounded_and_redirects_are_not_followed():
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            calls.append(self.path)
            self.rfile.read(int(self.headers['Content-Length']))
            if self.path.startswith('/slow'):
                self.send_response(200)
                self.end_headers()
                try:
                    for _ in range(10):
                        self.wfile.write(b' ')
                        self.wfile.flush()
                        time.sleep(.04)
                except (BrokenPipeError, ConnectionResetError): pass
                return
            if self.path.startswith('/redirect'):
                self.send_response(302)
                self.send_header('Location', 'http://external.example/model')
                self.end_headers()
                return
            body = response({'summary': '保留原验证状态。', 'items': []})
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f'http://127.0.0.1:{server.server_port}'
    try:
        rules = diagnose(config={})
        assert explain_diagnosis(rules, model_config=model(endpoint=root + '/v1'))['explanation']['source'] == 'local_model'
        started = time.monotonic()
        slow = explain_diagnosis(rules, model_config=model(endpoint=root + '/slow/v1', timeout_seconds=.1))
        assert slow['explanation']['fallback_reason'] == 'timeout'
        assert time.monotonic() - started < 1
        redirected = explain_diagnosis(rules, model_config=model(endpoint=root + '/redirect/v1'))
        assert redirected['explanation']['fallback_reason'] == 'unavailable'
        assert calls == ['/v1/chat/completions', '/slow/v1/chat/completions', '/redirect/v1/chat/completions']
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
