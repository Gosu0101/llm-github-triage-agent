"""Sequential VS Code classification v2 -> bug priority v0.3 -> saved results."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from importlib.resources import files
import json
import math
import os
from pathlib import Path
import sys
import time

from . import gemini, priority_review, review_bundle as base
from .priority_gemini import GeminiPriorityClient

CLASSIFICATION_VERSION = 'classification-v2-review'
CLASSIFICATION_SHA256 = '8c5975944151d7d8e6ed0f0b4a92fb715e15f38ffe1f63f2b2760bf0ec90b518'


class InputLengthError(ValueError):
    """Safe, structured preflight details; never source text or credentials."""

    def __init__(self, blocked_items, max_input_chars):
        super().__init__('Prompt plus input too long; truncation is forbidden')
        self.report = {'mode': 'rejected', 'reason': 'prompt_input_too_long',
                       'max_input_chars': max_input_chars, 'blocked_items': blocked_items,
                       'network_calls': 0, 'credentials_required': False}


def load(inputs, smoke=False, interval=13):
    """Validate both possible stages before reading credentials or creating output."""
    if not math.isfinite(interval) or not 0 <= interval <= 60:
        raise ValueError('Interval must be finite and between 0 and 60')
    plan = priority_review.load(inputs)
    prompt = files('triage_agent').joinpath('prompts', CLASSIFICATION_VERSION+'.txt').read_text(encoding='utf-8')
    if base.digest(prompt.encode('utf-8')) != CLASSIFICATION_SHA256:
        raise ValueError('Classification prompt hash mismatch')
    source_total = len(plan['rows'])
    if smoke:
        plan['rows'] = plan['rows'][:1]
    settings = gemini.GeminiSettings(allow_review=True, max_retries=1)
    settings.validate()
    priority = GeminiPriorityClient(interval=interval)
    prompt_size = max(len(prompt), len(priority.prompt))
    blocked = []
    for row in plan['rows']:
        size = sum(len(v) for v in row['input'].values())
        total = size + prompt_size
        if total > settings.max_input_chars:
            blocked.append({'case_id': row['case_id'], 'input_chars': size,
                            'prompt_chars': prompt_size, 'total_chars': total,
                            'excess_chars': total-settings.max_input_chars})
    if blocked:
        raise InputLengthError(blocked, settings.max_input_chars)
    plan.update(source_total=source_total, selection='smoke' if smoke else 'all',
                classification_prompt=prompt, classification_settings=settings,
                interval=interval)
    return plan, priority


def run(plan, output, classifier, priority):
    """Use real adapters and one shared transport interval, including retries.

    Like review_v2 this temporarily wraps the Gemini transport. This synchronous
    CLI is not a concurrent service; its finally block always restores transport.
    """
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    for name in ('classification/results', 'priority/results', 'results'):
        (output/name).mkdir(parents=True)
    class_meta = {'provider': 'google', 'model_requested': classifier.settings.model,
                  'prompt_version': CLASSIFICATION_VERSION,
                  'prompt_sha256': CLASSIFICATION_SHA256,
                  'settings': asdict(classifier.settings)}
    priority_meta = priority.metadata()
    base.write_json(output/'run-manifest.json', {
        'started_at': datetime.now(timezone.utc).isoformat(), 'mode': 'gemini',
        'selection': plan['selection'], 'source_total': plan['source_total'],
        'selected_count': len(plan['rows']), 'input_file_sha256': plan['input_file_sha256'],
        'classification': class_meta, 'priority': priority_meta,
        'minimum_attempt_start_interval_seconds': plan['interval'],
        'scope': 'Sequential development pipeline, not a formal evaluation.',
    })
    for name, text in [('classification-prompt.txt', plan['classification_prompt']),
                       ('priority-prompt.txt', priority.prompt)]:
        (output/name).write_text(text, encoding='utf-8', newline='\n')

    transport, previous = gemini._request, None

    def paced(*args):
        nonlocal previous
        if previous is not None:
            time.sleep(max(0.0, plan['interval'] - (time.monotonic() - previous)))
        previous = time.monotonic()
        return transport(*args)

    records, stopped = [], None
    gemini._request = paced
    try:
        for row in plan['rows']:
            identity = {k: row[k] for k in ('case_id', 'item_id', 'input_sha256')}
            value = row['input']
            classification = {**class_meta, **identity, 'status': 'unattempted',
                              'prediction': None, 'attempts': []}
            pr = {**priority_meta, **identity, 'status': 'unattempted',
                  'prediction': None, 'attempts': []}
            record = {**identity, 'status': 'unattempted', 'needs_review': None}
            if stopped is None:
                classification.update(classifier.generate(
                    **value, instruction=plan['classification_prompt'],
                    prompt_version=CLASSIFICATION_VERSION))
                if classification['status'] != 'success':
                    pr['status'] = 'upstream_failed'
                    stopped = {'case_id': row['case_id'], 'stage': 'classification',
                               'status': classification['status']}
                else:
                    prediction = classification['prediction']
                    if prediction['needs_review']:
                        pr['status'] = 'classification_review'
                    elif prediction['type'] != 'bug':
                        pr['status'] = 'not_applicable'
                    else:
                        pr.update(priority_review.infer_response(priority, value, provider='google'))
                        if pr['status'] != 'success':
                            stopped = {'case_id': row['case_id'], 'stage': 'priority',
                                       'status': pr['status']}
                record['status'] = stopped['status'] if stopped else 'success'
                if not stopped:
                    record['needs_review'] = (classification['prediction']['needs_review'] or
                                              bool(pr['prediction'] and pr['prediction']['needs_review']))
            pr['input'] = {'title_chars': len(value['title']),
                           'body_original_chars': len(value['body']),
                           'body_sent_chars': len(value['body']) if pr['attempts'] else 0,
                           'truncated': False, 'request_attempted': bool(pr['attempts'])}
            record.update(classification=classification, priority=pr)
            filename = row['case_id']+'.json'
            base.write_json(output/'classification/results'/filename, classification)
            base.write_json(output/'priority/results'/filename, pr)
            base.write_json(output/'results'/filename, record)
            records.append(record)
    finally:
        gemini._request = transport
    states = Counter(r['status'] for r in records)
    calls = {stage: sum(len(r[stage]['attempts']) for r in records)
             for stage in ('classification', 'priority')}
    base.write_json(output/'summary.json', {
        'mode': 'gemini', 'total': len(records),
        'network_calls': sum(calls.values()),
        'classification_calls': calls['classification'], 'priority_calls': calls['priority'],
        'success': states['success'], 'review': sum(r['needs_review'] is True for r in records),
        'failed': sum(states[s] for s in priority_review.FAILURES),
        'unattempted': states['unattempted'], 'stopped': stopped,
        'priority_states': dict(Counter(r['priority']['status'] for r in records)),
        'human_labels_status': 'not_collected',
    })
    return 2 if stopped else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--interval', type=float, default=13)
    parser.add_argument('--smoke', action='store_true', help='One input; up to two stages, plus classification retry')
    args = parser.parse_args(argv)
    try:
        if args.execute != bool(args.output):
            raise ValueError('Execute and a new output path must be supplied together')
        if args.output is not None and args.output.exists():
            raise ValueError('Output already exists')
        plan, priority = load(args.inputs, args.smoke, args.interval)
        if not args.execute:
            print(json.dumps({'mode': 'validated', 'selection': plan['selection'],
                              'classification_calls': len(plan['rows']),
                              'priority_calls_max': len(plan['rows']),
                              'classification_retry_limit': 1,
                              'transport_attempts_max': 3 * len(plan['rows']),
                              'priority_calls_note': 'Only successful non-review bug predictions are called.',
                              'network_calls': 0, 'credentials_required': False,
                              'classification_prompt_sha256': CLASSIFICATION_SHA256,
                              'priority_prompt_sha256': priority.metadata()['prompt_sha256']}))
            return 0
        if args.env_file:
            if not args.env_file.is_file():
                raise ValueError('Missing env file')
            from .config import load_env_file
            load_env_file(args.env_file)
        key = os.environ.get('GEMINI_API_KEY', '')
        classifier = gemini.GeminiClient(key, plan['classification_settings'])
        priority.set_api_key(key)
        code = run(plan, args.output, classifier, priority)
        summary = priority_review.read_json(args.output/'summary.json')
        print(json.dumps({'mode': 'gemini', 'network_calls': summary['network_calls'],
                          'output': str(args.output)}))
        return code
    except InputLengthError as error:
        print(json.dumps(error.report, ensure_ascii=False), file=sys.stderr)
        return 2
    except (ValueError, OSError, RecursionError):
        print('Pipeline validation failed; check input, output and GEMINI_API_KEY configuration.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
