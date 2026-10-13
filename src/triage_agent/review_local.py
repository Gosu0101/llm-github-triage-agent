"""Portable LM Studio classification test. Offline unless --execute is supplied."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


from . import review_bundle as run_v1

SCHEMA = {
    'type': 'object',
    'properties': {
        'type': {'type': ['string', 'null'], 'enum': [*run_v1.TYPES, None]},
        'rationale': {'type': 'string'},
        'needs_review': {'type': 'boolean'},
    },
    'required': ['type', 'rationale', 'needs_review'],
    'additionalProperties': False,
}


class LocalError(Exception):
    def __init__(self, status, http_status=None):
        super().__init__(status)
        self.status = status
        self.http_status = http_status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check_base_url(address):
    value = urlsplit(address)
    if (value.scheme != 'http' or value.hostname not in ('127.0.0.1', 'localhost', '::1')
            or value.username or value.password or value.query or value.fragment
            or value.path.rstrip('/') != '/v1'):
        raise ValueError('Use a local HTTP endpoint such as http://127.0.0.1:1234/v1')
    _ = value.port
    return address.rstrip('/')


def strict_json(text):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('Duplicate field')
            value[key] = item
        return value

    def constant(value):
        raise ValueError('Nonstandard JSON')

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def request_json(url, payload, timeout):
    request = Request(url, data=json.dumps(payload).encode('utf-8'), method='POST',
                      headers={'Content-Type': 'application/json'})
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(2_000_001)
    except HTTPError as error:
        code = error.code
        error.close()
        raise LocalError('api_error', code) from None
    except (URLError, TimeoutError, OSError):
        raise LocalError('network_error') from None
    if len(raw) > 2_000_000:
        raise LocalError('invalid_response')
    try:
        value = strict_json(raw.decode('utf-8'))
    except (ValueError, UnicodeError, RecursionError):
        raise LocalError('invalid_json') from None
    if not isinstance(value, dict):
        raise LocalError('invalid_response')
    return value


def valid_prediction(value):
    if not isinstance(value, dict) or set(value) != {'type', 'rationale', 'needs_review'}:
        return False
    if not isinstance(value['rationale'], str) or not value['rationale'].strip():
        return False
    if type(value['needs_review']) is not bool:
        return False
    return ((value['type'] is None and value['needs_review']) or
            (isinstance(value['type'], str) and value['type'] in run_v1.TYPES
             and not value['needs_review']))


class LocalClient:
    def __init__(self, base_url, model, timeout, max_tokens):
        self.base_url = check_base_url(base_url)
        self.model = model
        self.timeout = timeout
        self.settings = {'temperature': 0, 'max_tokens': max_tokens, 'stream': False,
                         'structured_output': 'json_schema_strict', 'automatic_retries': 0}

    def generate(self, *, title, body, instruction, prompt_version):
        model_input = {'title': title, 'body': body}
        payload = {
            'model': self.model,
            'messages': [{'role': 'system', 'content': instruction},
                         {'role': 'user', 'content': json.dumps(model_input, ensure_ascii=False)}],
            'temperature': self.settings['temperature'], 'max_tokens': self.settings['max_tokens'],
            'stream': False,
            'response_format': {'type': 'json_schema', 'json_schema': {
                'name': 'issue_classification', 'strict': True, 'schema': SCHEMA}},
        }
        record = {
            'checked_at': datetime.now(timezone.utc).isoformat(), 'provider': 'lm-studio-local',
            'model_requested': self.model, 'prompt_version': prompt_version,
            'prompt_sha256': run_v1.digest(instruction.encode('utf-8')),
            'input_sha256': run_v1.input_hash(model_input), 'settings': self.settings,
            'input': {'title_chars': len(title), 'body_original_chars': len(body),
                      'body_sent_chars': len(body), 'truncated': False, 'request_attempted': True},
            'prediction': None, 'attempts': [],
        }
        started = time.monotonic()
        http_status = None
        try:
            response = request_json(self.base_url + '/chat/completions', payload, self.timeout)
            http_status = 200
            choices = response.get('choices')
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                raise LocalError('invalid_response', 200)
            choice = choices[0]
            record['finish_reason'] = choice.get('finish_reason')
            if choice.get('finish_reason') != 'stop':
                raise LocalError('incomplete_response', 200)
            message = choice.get('message')
            if not isinstance(message, dict) or not isinstance(message.get('content'), str):
                raise LocalError('invalid_response', 200)
            try:
                prediction = strict_json(message['content'])
            except (ValueError, RecursionError):
                raise LocalError('invalid_json', 200) from None
            if not valid_prediction(prediction):
                raise LocalError('invalid_schema', 200)
            record.update(status='success', prediction=prediction)
            if isinstance(response.get('model'), str):
                record['model_returned'] = response['model']
            usage = response.get('usage')
            if isinstance(usage, dict):
                record['usage'] = {key: usage[key] for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                                   if type(usage.get(key)) is int and usage[key] >= 0}
        except LocalError as error:
            record['status'] = error.status
            http_status = error.http_status
        record['latency_seconds'] = round(time.monotonic() - started, 3)
        record['attempts'] = [{'number': 1, 'status': record['status'], 'http_status': http_status,
                               'latency_seconds': record['latency_seconds']}]
        return record


def execute(bundle, output, client, smoke=False):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    phases = ['v1'] if smoke else ['v0', 'v1']
    manifests = bundle['manifest']
    run_v1.write_json(output / 'run-manifest.json', {
        'execution_kind': 'local', 'started_at': datetime.now(timezone.utc).isoformat(),
        'mode': 'smoke' if smoke else 'paired', 'base_url': client.base_url,
        'model': client.model, 'settings': client.settings, 'timeout_seconds': client.timeout,
        'bundle_hashes': manifests['hashes'],
        'runtime_note': 'Record LM Studio version, quantization, context, GPU offload and reasoning settings separately.',
        'baseline': 'v0 rerun on this local model; Gemini v0 is not the comparison baseline.',
    })
    results = {'v0': [], 'v1': []}
    stopped = None
    for phase in phases:
        directory = output / phase
        directory.mkdir()
        prompt = (bundle['root'] / 'reference/classification-v0-review.txt').read_text(encoding='utf-8') if phase == 'v0' else bundle['prompt']
        rows = bundle['dev'][:1] if smoke else bundle['dev'] + (bundle['reserve'] if phase == 'v1' else [])
        for index, row in enumerate(rows, 1):
            record = client.generate(title=row['input']['title'], body=row['input']['body'],
                                     instruction=prompt, prompt_version='classification-' + phase + '-review')
            record.update(item_id=row['item_id'], execution_kind='local',
                          execution_group='dev' if index <= len(bundle['dev']) else 'reserve',
                          dataset_version=manifests['dataset_version'])
            run_v1.write_json(directory / (row['item_id'].split('#')[1] + '.json'), record)
            results[phase].append(record)
            print(f"{phase} {index}/{len(rows)} {row['item_id']}: {record['status']} "
                  f"{(record['prediction'] or {}).get('type')}", flush=True)
            if record['status'] != 'success':
                stopped = {'phase': phase, 'item_id': row['item_id'], 'status': record['status'],
                           'http_status': record['attempts'][0]['http_status']}
                break
        if stopped:
            break
    summary = {'mode': 'smoke' if smoke else 'paired', 'execution_kind': 'local',
               'model': client.model, 'calls': sum(len(rows) for rows in results.values()),
               'stopped': stopped, 'scope': 'Development reference; not final evaluation.'}
    if smoke:
        summary['result'] = results['v1'][0]
        print(json.dumps(summary['result']['prediction'], ensure_ascii=False), flush=True)
    else:
        summary['phases'] = {}
        for phase in ('v0', 'v1'):
            report, table = run_v1.compare(results[phase], bundle['gold'],
                                          results['v0'] if phase == 'v1' else [],
                                          manifests['reserve_ids'] if phase == 'v1' else [])
            summary['phases'][phase] = report
            with (output / (phase + '-comparison.csv')).open('x', encoding='utf-8-sig', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(table[0]))
                writer.writeheader()
                writer.writerows(table)
        summary['paired_predictions_complete'] = all(
            len(results[phase]) >= 15 and all(row['status'] == 'success' and row['prediction']['type'] is not None
                                             for row in results[phase][:15]) for phase in ('v0', 'v1'))
        summary['interpretation'] = 'Only interpret prompt improvement when paired dev predictions are complete. Reserve cases are supplementary.'
    run_v1.write_json(output / 'summary.json', summary)
    print('Results: ' + str(output.resolve()), flush=True)
    return summary
