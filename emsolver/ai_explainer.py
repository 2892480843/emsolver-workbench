"""Optional bounded loopback explanation; the numerical result is never editable."""
from __future__ import annotations

import copy
from dataclasses import dataclass
import http.client
import ipaddress
import json
import re
import socket
import threading
import time
from urllib.parse import urlsplit


TEMPLATE_VERSION = 'diagnosis_explanation_v3'


@dataclass(frozen=True)
class LocalModelConfig:
    endpoint: str
    model: str
    model_version: str
    license_id: str
    license_approved: bool = False
    timeout_seconds: float = 3.0
    max_response_bytes: int = 65536

    def __post_init__(self):
        parsed = urlsplit(self.endpoint)
        if parsed.scheme != 'http' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Local model endpoint must be plain HTTP on loopback without credentials or query')
        host = parsed.hostname
        try:
            loopback = host == 'localhost' or ipaddress.ip_address(host).is_loopback
            parsed.port
        except ValueError:
            loopback = False
        if not loopback or (parsed.path and not re.fullmatch(r'/[A-Za-z0-9_./-]*', parsed.path)) or '..' in parsed.path:
            raise ValueError('Only literal loopback addresses or localhost are permitted')
        if type(self.timeout_seconds) not in (int, float) or not 0 < self.timeout_seconds <= 10:
            raise ValueError('Timeout must be between zero and ten seconds')
        if type(self.max_response_bytes) is not int or not 1024 <= self.max_response_bytes <= 65536:
            raise ValueError('Response limit must be between 1024 and 65536 bytes')
        for value in (self.model, self.model_version, self.license_id):
            if not isinstance(value, str) or not 1 <= len(value) <= 200 or any(ord(char) < 32 for char in value):
                raise ValueError('Reviewed model, version and license metadata are required')
            if value.startswith(('/', '\\')) or any(word in value for word in ('/Users/', '/home/', 'file://')):
                raise ValueError('Private paths are not model metadata')
        if type(self.license_approved) is not bool:
            raise ValueError('License approval must be explicit')


def _request(config, body):
    parsed = urlsplit(config.endpoint)
    # Avoid proxies, DNS rebinding and redirects. localhost is pinned to loopback.
    host = '127.0.0.1' if parsed.hostname == 'localhost' else parsed.hostname
    path = parsed.path.rstrip('/')
    if not path.endswith('/chat/completions'):
        path += '/chat/completions' if path.endswith('/v1') else '/v1/chat/completions'
    connection = http.client.HTTPConnection(host, parsed.port or 80, timeout=config.timeout_seconds)
    deadline = time.monotonic() + config.timeout_seconds
    expired = threading.Event()
    timer = None
    try:
        connection.connect()
        connected_socket = connection.sock
        def expire():
            expired.set()
            try:
                connected_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        # A socket's per-read timeout alone permits an endless trickle. Enforce
        # one total deadline across response headers and the bounded body.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        timer = threading.Timer(remaining, expire)
        timer.daemon = True
        timer.start()
        connection.request('POST', path, body=body, headers={'Content-Type': 'application/json', 'Connection': 'close'})
        response = connection.getresponse()
        if response.status != 200:
            raise OSError('Local model did not return a successful response')
        raw = response.read(config.max_response_bytes + 1)
        if expired.is_set():
            raise TimeoutError()
        if len(raw) > config.max_response_bytes:
            raise ValueError('Response limit exceeded')
        return raw
    except (OSError, http.client.HTTPException):
        if expired.is_set():
            raise TimeoutError() from None
        raise
    finally:
        if timer:
            timer.cancel()
        connection.close()


def _safe_text(value, limit):
    if not isinstance(value, str) or not 1 <= len(value) <= limit:
        raise ValueError('Invalid explanation text')
    # Treat all model prose as untrusted display-only content. Reject obvious
    # code/markup/path payloads rather than echoing them through reports.
    if any(word in value for word in ('```', '<', '>', '/Users/', '/home/', 'file://', 'exec(', 'eval(', '#!')):
        raise ValueError('Executable, markup or private path content is not allowed')
    if any(ord(char) < 32 and char not in '\n\t' for char in value):
        raise ValueError('Control characters are not allowed')
    return value


def _parse(raw, config, rule_ids):
    if not isinstance(raw, bytes) or len(raw) > config.max_response_bytes:
        raise ValueError('Invalid bounded response')
    value = json.loads(raw)
    content = value['choices'][0]['message']['content']
    value = json.loads(content)
    if not isinstance(value, dict) or set(value) != {'summary', 'items'}:
        raise ValueError('Invalid explanation schema')
    summary = _safe_text(value['summary'], 2000)
    items = value['items']
    if not isinstance(items, list) or len(items) > 20:
        raise ValueError('Invalid explanation items')
    seen = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {'rule_id', 'text'} or item['rule_id'] not in rule_ids or item['rule_id'] in seen:
            raise ValueError('Invalid or duplicate rule reference')
        seen.add(item['rule_id'])
        _safe_text(item['text'], 1500)
    return summary, items


def explain_diagnosis(diagnosis, job=None, model_config=None, *, transport=None):
    """Return a copy with display-only prose; no matrix, paths or job text leave."""
    result = copy.deepcopy(diagnosis)
    suggestions = diagnosis.get('suggestions') or []
    explanation = {'source': 'rules', 'summary': '；'.join(item['reason'] for item in suggestions),
                   'items': [{'rule_id': item['rule_id'], 'text': item['reason']} for item in suggestions],
                   'model': None, 'template_version': TEMPLATE_VERSION, 'fallback_reason': 'not_configured'}
    result['explanation'] = explanation
    if model_config is None:
        return result
    if isinstance(model_config, dict):
        try:
            model_config = LocalModelConfig(**model_config)
        except (TypeError, ValueError):
            explanation['fallback_reason'] = 'invalid_configuration'
            return result
    if not isinstance(model_config, LocalModelConfig):
        explanation['fallback_reason'] = 'invalid_configuration'
        return result
    explanation['model'] = {'id': model_config.model, 'version': model_config.model_version,
                            'license_id': model_config.license_id, 'license_approved': model_config.license_approved}
    if not model_config.license_approved:
        explanation['fallback_reason'] = 'license_not_approved'
        return result
    # Only internally generated titles and reasons are sent. Arbitrary error
    # messages, input paths, names, eigenpairs and raw matrix data are excluded.
    safe = {'rule_version': diagnosis.get('rule_version'),
            'rules': [{'id': item['rule_id'], 'title': item['title'], 'reason': item['reason'],
                       'prerequisites': item.get('prerequisites', [])} for item in suggestions[:20]]}
    # Full conversational examples teach the JSON envelope without supplying
    # the answer to the final case or weakening the untrusted-output parser.
    examples = [
        ({'rule_version': 'diagnosis_v2', 'rules': [
            {'id': 'non_hermitian', 'title': '核对三角声明和源数据',
             'reason': '输入未通过厄密检查；修正数据来源后新建输入版本。',
             'prerequisites': ['使用实对称或复厄密输入', '上、下三角存储必须明确声明']}]},
         {'summary': '输入的数学结构需要先核对。', 'items': [
             {'rule_id': 'non_hermitian', 'text': '先核对矩阵及三角存储声明。只有实对称或复厄密输入适用；修正来源后应创建新输入版本。'}]}),
        ({'rule_version': 'diagnosis_v2', 'rules': [
            {'id': 'memory_exhausted', 'title': '检查资源构成',
             'reason': '资源使用触及冻结预算；资源硬限额不扩大。',
             'prerequisites': ['候选维数满足后端限制', '预估与进程树监控满足原资源预算']}]},
         {'summary': '需要在原有资源预算内重新检查计算规模。', 'items': [
             {'rule_id': 'memory_exhausted', 'text': '检查哪些资源触及上限，并核对可负担的候选维数。仍须满足后端限制及原资源预算，不能通过提高硬限额来忽略问题。'}]}),
    ]
    messages = [{'role': 'system', 'content':
                 '你只解释最后一条用户消息中的诊断规则，使用简体中文。严格仿照示例回答，'
                 '只输出一个JSON对象，顶层只有summary和items。每项只有rule_id和text。'
                 'rule_id只取最后输入中的id。保留适用前提。不要Markdown、代码围栏、命令、'
                 '新配置、数值答案或通过结论，不能改变数值状态。'}]
    for example, answer in examples:
        messages.extend([{'role': 'user', 'content': json.dumps(example, ensure_ascii=False)},
                         {'role': 'assistant', 'content': json.dumps(answer, ensure_ascii=False)}])
    messages.append({'role': 'user', 'content': json.dumps(safe, ensure_ascii=False)})
    body = json.dumps({'model': model_config.model, 'temperature': 0, 'max_tokens': 1200,
                       'response_format': {'type': 'json_object'}, 'messages': messages},
                      ensure_ascii=False, allow_nan=False).encode('utf-8')
    try:
        raw = (transport or _request)(model_config, body)
        summary, items = _parse(raw, model_config, {item['rule_id'] for item in suggestions})
    except (socket.timeout, TimeoutError):
        explanation['fallback_reason'] = 'timeout'
    except (ValueError, TypeError, KeyError, IndexError, UnicodeError, RecursionError):
        explanation['fallback_reason'] = 'invalid_response'
    except (OSError, http.client.HTTPException):
        explanation['fallback_reason'] = 'unavailable'
    else:
        explanation.update(source='local_model', summary=summary, items=items, fallback_reason=None)
        result['ai_available'] = True
    return result
