"""Offline priority planning and mock-only execution. No live provider is wired."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from http.client import HTTPException
import json
from pathlib import Path
import re
import sys
import time

from . import priority_contract as contract
from . import review_bundle as base, review_local

FAILURES = {'api_error', 'network_error', 'timeout', 'rate_limited', 'server_error',
            'auth_error', 'invalid_json', 'invalid_response', 'invalid_schema',
            'incomplete_response', 'input_too_long'}


def read_json(path):
    try:
        return contract.strict_json(Path(path).read_text(encoding='utf-8-sig'))
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('Invalid JSON input file') from None


def load(inputs_path, classifications_path=None, max_input_chars=12000):
    """Join saved predictions to exact title/body hashes, never to gold labels."""
    package = read_json(inputs_path)
    if (not isinstance(package, dict)
            or package.get('schema_version') != 'priority-preparation-inputs-v0'
            or not isinstance(package.get('cases'), list) or not package['cases']):
        raise ValueError('Expected a nonempty priority preparation input bundle')
    rows, ids, cases = [], set(), set()
    for row in package['cases']:
        if not isinstance(row, dict) or set(row) != {'case_id', 'item_id', 'origin', 'input', 'input_sha256'}:
            raise ValueError('Invalid input wrapper; labels and predictions are forbidden')
        case_id, item_id, value = row['case_id'], row['item_id'], row['input']
        if (not isinstance(case_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', case_id)
                or not isinstance(item_id, str) or not item_id.strip()
                or not isinstance(row['origin'], str) or not row['origin'].strip()):
            raise ValueError('Invalid case identifier or origin')
        if re.fullmatch(r'CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]', case_id, re.IGNORECASE):
            raise ValueError('Case identifier is a reserved Windows filename')
        if case_id.casefold() in cases or item_id in ids:
            raise ValueError('Duplicate case_id or item_id')
        ids.add(item_id); cases.add(case_id.casefold())
        if (not isinstance(value, dict) or set(value) != {'title', 'body'}
                or not all(isinstance(v, str) for v in value.values())):
            raise ValueError(f'{case_id}: input must contain exactly string title/body')
        value = {'title': value['title'], 'body': value['body']}
        if len(value['title']) + len(value['body']) > max_input_chars:
            raise ValueError(f'{case_id}: input too long; truncation is forbidden')
        if row['input_sha256'] != base.input_hash(value):
            raise ValueError(f'{case_id}: input hash mismatch')
        rows.append(dict(row, input=value, state='not_ready'))

    records, sources = {}, []
    if classifications_path is not None:
        path = Path(classifications_path)
        paths = sorted(path.glob('*.json')) if path.is_dir() else [path]
        for source in paths:
            if path.is_dir():
                values = [read_json(source)]
            else:
                try:
                    values = [contract.strict_json(line) for line in source.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
                except (ValueError, UnicodeError, RecursionError):
                    raise ValueError('Invalid classification JSONL') from None
            sources.append({'path': str(source), 'sha256': base.digest(source.read_bytes())})
            for record in values:
                if (not isinstance(record, dict) or not isinstance(record.get('item_id'), str)
                        or record['item_id'] in records):
                    raise ValueError('Invalid or duplicate classification item_id')
                records[record['item_id']] = record
    for row in rows:
        record = records.get(row['item_id'])
        if record is None:
            continue
        if record.get('input_sha256') != row['input_sha256']:
            raise ValueError(f"{row['case_id']}: classification input hash mismatch")
        status, prediction = record.get('status'), record.get('prediction')
        if status == 'success':
            if not review_local.valid_prediction(prediction):
                raise ValueError(f"{row['case_id']}: invalid classification prediction")
            row['state'] = ('classification_review' if prediction['needs_review'] else
                            'eligible' if prediction['type'] == 'bug' else 'not_applicable')
        elif isinstance(status, str) and status in FAILURES and prediction is None:
            row['state'] = 'upstream_failed'
        else:
            raise ValueError(f"{row['case_id']}: invalid classification status or prediction")
    return {'rows': rows, 'eligible_count': sum(r['state'] == 'eligible' for r in rows),
            'input_file_sha256': base.digest(Path(inputs_path).read_bytes()),
            'classification_sources': sources,
            'ignored_classifications': len(set(records) - ids)}


def mock_request(path, plan):
    """Validate all mock fixtures before any result directory is created."""
    fixtures = read_json(path)
    eligible = [r for r in plan['rows'] if r['state'] == 'eligible']
    if not isinstance(fixtures, list) or len(fixtures) != len(eligible):
        raise ValueError('One ordered mock response per eligible case is required')
    for fixture, row in zip(fixtures, eligible):
        if (not isinstance(fixture, dict) or set(fixture) != {'case_id', 'input_sha256', 'response'}
                or fixture['case_id'] != row['case_id'] or fixture['input_sha256'] != row['input_sha256']):
            raise ValueError('Mock response order or input hash mismatch')
    iterator = iter(fixtures)

    def request(**model_input):
        fixture = next(iterator)
        if base.input_hash(model_input) != fixture['input_sha256']:
            raise ValueError('Mock request input mismatch')
        return fixture['response']

    return request


def infer_mock(request, value):
    """Use real parsing/error handling around an injected simulated transport."""
    started = time.monotonic()
    record = {'provider': 'mock', 'model_requested': None, 'prediction': None, 'attempts': []}
    http_status = None
    try:
        response = request(title=value['title'], body=value['body'])
        http_status = 200
        if not isinstance(response, dict):
            raise review_local.LocalError('invalid_response', 200)
        usage = response.get('usage')
        if isinstance(usage, dict):
            record['usage'] = {k: usage[k] for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                               if type(usage.get(k)) is int and usage[k] >= 0}
        choices = response.get('choices')
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise review_local.LocalError('invalid_response', 200)
        choice = choices[0]
        record['finish_reason'] = choice.get('finish_reason')
        if record['finish_reason'] != 'stop':
            raise review_local.LocalError('incomplete_response', 200)
        message = choice.get('message')
        if not isinstance(message, dict) or not isinstance(message.get('content'), str):
            raise review_local.LocalError('invalid_response', 200)
        try:
            prediction = contract.strict_json(message['content'])
        except (ValueError, RecursionError):
            raise review_local.LocalError('invalid_json', 200) from None
        if not contract.valid_prediction(prediction):
            raise review_local.LocalError('invalid_schema', 200)
        record.update(status='success', prediction=prediction)
    except TimeoutError:
        record['status'] = 'timeout'
    except (OSError, HTTPException):
        record['status'] = 'network_error'
    except review_local.LocalError as error:
        record['status'] = error.status
        http_status = error.http_status
    record['latency_seconds'] = round(time.monotonic() - started, 3)
    record['attempts'] = [{'number': 1, 'status': record['status'], 'http_status': http_status,
                           'latency_seconds': record['latency_seconds']}]
    return record


def run(plan, output, request, *, mock_responses_sha256=None):
    """Persist a mock run. Caller supplies a simulated request, never a live API."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    results = output/'results'; results.mkdir()
    base.write_json(output/'run-manifest.json', {
        'started_at': datetime.now(timezone.utc).isoformat(), 'mode': 'mock',
        'provider': 'mock', 'contract_version': contract.CONTRACT_VERSION,
        'contract_status': 'provisional', 'automatic_retries': 0, 'network_calls': 0,
        'input_file_sha256': plan['input_file_sha256'],
        'classification_sources': plan['classification_sources'],
        'mock_responses_sha256': mock_responses_sha256,
        'scope': 'Synthetic execution only. No accuracy or final policy claim.',
    })
    records, stopped = [], None
    for row in plan['rows']:
        record = {'case_id': row['case_id'], 'item_id': row['item_id'],
                  'input_sha256': row['input_sha256'], 'provider': 'mock',
                  'status': row['state'], 'prediction': None, 'attempts': []}
        if row['state'] == 'eligible':
            if stopped:
                record['status'] = 'unattempted'
            else:
                record.update(infer_mock(request, row['input']))
                if record['status'] != 'success':
                    stopped = {'case_id': row['case_id'], 'status': record['status']}
        base.write_json(results/(row['case_id']+'.json'), record)
        records.append(record)
    states = Counter(r['status'] for r in records)
    successes = [r['prediction'] for r in records if r['status'] == 'success']
    summary = {'mode': 'mock', 'total': len(records), 'eligible_count': plan['eligible_count'],
               'simulated_calls': sum(len(r['attempts']) for r in records), 'network_calls': 0,
               'success': len(successes), 'review': sum(p['needs_review'] for p in successes),
               'failed': sum(states[s] for s in FAILURES), 'unattempted': states['unattempted'],
               'excluded': {s: states[s] for s in ('not_applicable', 'classification_review', 'upstream_failed', 'not_ready')},
               'grades': {g: sum(p['priority'] == g for p in successes) for g in contract.GRADES},
               'stopped': stopped, 'human_labels_status': 'not_collected'}
    base.write_json(output/'summary.json', summary)
    return 2 if stopped else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--classifications', type=Path, help='Saved primary result directory or JSONL; never gold')
    parser.add_argument('--mock-responses', type=Path, help='Run with simulated responses only')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        if bool(args.mock_responses) != bool(args.output):
            raise ValueError('--mock-responses and --output must be supplied together')
        plan = load(args.inputs, args.classifications)
        if args.mock_responses:
            request = mock_request(args.mock_responses, plan)
            code = run(plan, args.output, request,
                       mock_responses_sha256=base.digest(args.mock_responses.read_bytes()))
            print(json.dumps({'mode': 'mock', 'network_calls': 0, 'output': str(args.output)}))
            return code
        print(json.dumps({'mode': 'validated', 'contract_status': 'provisional',
                          'total': len(plan['rows']), 'eligible_count': plan['eligible_count'],
                          'states': dict(Counter(r['state'] for r in plan['rows'])),
                          'ignored_classifications': plan['ignored_classifications'],
                          'network_calls': 0, 'credentials_required': False}))
        return 0
    except (ValueError, OSError, RecursionError) as error:
        # Do not print mock content, source reports, or exception details.
        print('Priority validation failed; check input, classification and output paths.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
