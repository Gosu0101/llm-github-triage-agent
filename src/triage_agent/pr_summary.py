"""Validate saved PR inputs offline, or explicitly execute Gemini summaries."""
from __future__ import annotations

import argparse
from collections import Counter
from importlib.resources import files
import json
import math
import os
from pathlib import Path
import re
import sys
import time

from . import feature_report, gemini, priority_contract, review_bundle as base

PROMPT_VERSION = 'pr-summary-v0.1'
DEFAULT_MODEL = 'gemini-3.6-flash'
LIST_FIELDS = ('key_changes', 'affected_areas', 'tests', 'risks', 'breaking_changes')
SCHEMA = {'type': 'object', 'properties': {
    'summary': {'type': 'string', 'minLength': 1},
    **{k: {'type': 'array', 'items': {'type': 'string', 'minLength': 1}} for k in LIST_FIELDS},
    'insufficient_information': {'type': 'boolean'}},
    'required': ['summary', *LIST_FIELDS, 'insufficient_information'], 'additionalProperties': False}


def extract(data):
    """Retain predictions only after envelope, strict JSON and contract checks."""
    if not isinstance(data, dict):
        return 'invalid_response', None
    if data.get('status') in ('incomplete', 'in_progress', 'failed', 'cancelled', 'requires_action'):
        return 'incomplete_response', None
    if data.get('status') != 'completed' or not isinstance(data.get('steps'), list):
        return 'invalid_response', None
    parts = []
    for step in data['steps']:
        if not isinstance(step, dict):
            return 'invalid_response', None
        if step.get('type') != 'model_output':
            continue
        if not isinstance(step.get('content'), list):
            return 'invalid_response', None
        for block in step['content']:
            if not isinstance(block, dict):
                return 'invalid_response', None
            if block.get('type') == 'text' and isinstance(block.get('text'), str):
                parts.append(block['text'])
    if not parts:
        return 'invalid_response', None
    try:
        prediction = priority_contract.strict_json(''.join(parts))
    except (ValueError, RecursionError):
        return 'invalid_json', None
    if not feature_report.summary_prediction(prediction):
        return 'invalid_schema', None
    return 'success', prediction


def fit_input_patches(value, budget):
    """Keep PR metadata and whole patches in source order, skipping patches that do not fit."""
    omitted = {file['filename'] for file in value['files'] if file['patch'] is not None}
    fitted = feature_report.pr_without_patches(value, omitted)
    size = len(json.dumps(fitted, ensure_ascii=False))
    if size > budget:
        return None, 'pr_metadata_too_long'

    for index, file in enumerate(value['files']):
        if file['patch'] is None:
            continue
        extra = (len(json.dumps(file, ensure_ascii=False))
                 - len(json.dumps(fitted['files'][index], ensure_ascii=False)))
        if size + extra <= budget:
            fitted['files'][index] = file
            omitted.remove(file['filename'])
            size += extra
    if not any(file['patch'] for file in fitted['files']):
        return None, 'no_patch_fits'

    coverage = {'omitted_patch_files': [file['filename'] for file in value['files']
                                        if file['filename'] in omitted],
                'model_input_sha256': base.input_hash(fitted)}
    return fitted, coverage


def reduced_inputs(rows, prompt):
    return [{'case_id': row['case_id'], **row['input_coverage'],
             'total_chars': len(prompt) + len(json.dumps(row['input'], ensure_ascii=False))}
            for row in rows if 'input_coverage' in row]


def load(path, case_id=None, *, model=DEFAULT_MODEL, interval=13, fit_patches=False):
    settings = gemini.GeminiSettings(model=model, max_output_tokens=4096, timeout_seconds=60)
    settings.validate()
    if not math.isfinite(interval) or not 0 <= interval <= 60:
        raise ValueError('Invalid request interval')
    rows = feature_report.pr_inputs(path)
    total = len(rows)
    if case_id is not None:
        rows = [r for r in rows if r['case_id'] == case_id]
        if len(rows) != 1:
            raise ValueError('Expected exactly one matching PR case')
    # These identifiers become filenames. Validate before creating any output.
    ids = set()
    for row in rows:
        name = row['case_id']
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name)
                or re.fullmatch(r'CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]', name, re.I)
                or name.casefold() in ids):
            raise ValueError('Unsafe or duplicate case identifier')
        ids.add(name.casefold())
    prompt = files('triage_agent').joinpath('prompts', PROMPT_VERSION+'.txt').read_text(encoding='utf-8')
    blocked = []
    for row in rows:
        size = len(prompt) + len(json.dumps(row['input'], ensure_ascii=False))
        if size <= settings.max_input_chars:
            continue
        rejection = {'case_id': row['case_id'], 'total_chars': size,
                     'excess_chars': size-settings.max_input_chars}
        if fit_patches:
            fitted, coverage = fit_input_patches(row['input'], settings.max_input_chars-len(prompt))
            if fitted is not None:
                row.update(input=fitted, input_coverage=coverage)
                continue
            rejection['reason'] = coverage
        blocked.append(rejection)
    if blocked:
        raise LengthError({'mode': 'rejected', 'reason': 'prompt_input_too_long',
                           'max_input_chars': settings.max_input_chars, 'blocked_items': blocked,
                           'network_calls': 0, 'credentials_required': False})
    return rows, total, prompt, settings


class LengthError(ValueError):
    def __init__(self, report):
        super().__init__('PR input exceeds character budget; no truncation')
        self.report = report


def redact(value, key):
    if isinstance(value, str):
        return value.replace(key, '[REDACTED]')
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    if isinstance(value, dict):
        return {redact(k, key): redact(v, key) for k, v in value.items()}
    return value


def run(rows, total, prompt, settings, output, key, interval):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output/'results').mkdir()
    metadata = {'mode': 'gemini', 'provider': 'google', 'model_requested': settings.model,
                'prompt_version': PROMPT_VERSION, 'prompt_sha256': base.digest(prompt.encode())}
    base.write_json(output/'run-manifest.json', {**metadata, 'source_total': total,
        'selected_case_ids': [r['case_id'] for r in rows], 'automatic_retries': 0,
        'max_input_chars': settings.max_input_chars, 'max_output_tokens': settings.max_output_tokens,
        'minimum_request_interval_seconds': interval, 'store': False,
        'reduced_inputs': reduced_inputs(rows, prompt),
        'scope': 'Development PR summaries; not a quality evaluation.'})
    (output/'prompt.txt').write_text(prompt, encoding='utf-8')
    states, calls, previous_start, stopped = Counter(), 0, None, False
    for row in rows:
        record = {**metadata, **{k: row[k] for k in ('case_id', 'item_id', 'input_sha256')},
                  'status': 'unattempted', 'prediction': None, 'network_calls': 0}
        if 'input_coverage' in row:
            record['input_coverage'] = row['input_coverage']
        if not stopped:
            payload = {'model': settings.model,
                'input': json.dumps(row['input'], ensure_ascii=False),
                'system_instruction': prompt, 'store': False,
                'response_format': {'type': 'text', 'mime_type': 'application/json', 'schema': SCHEMA},
                'generation_config': {'temperature': 0, 'max_output_tokens': settings.max_output_tokens,
                                      'thinking_level': 'low'}}
            if previous_start is not None:
                time.sleep(max(0, interval-(time.monotonic()-previous_start)))
            previous_start = time.monotonic()
            calls += 1
            record['network_calls'] = 1
            try:
                response = gemini._request(payload, key, settings.timeout_seconds)
                record['status'], prediction = extract(response)
                record['prediction'] = redact(prediction, key)
                if isinstance(response, dict):
                    model = response.get('model')
                    if isinstance(model, str) and re.fullmatch(r'gemini-[a-z0-9.-]+', model):
                        record['model_returned'] = redact(model, key)
                    usage = response.get('usage')
                    if isinstance(usage, dict):
                        record['usage'] = {k: usage[k] for k in gemini.USAGE_FIELDS
                                           if type(usage.get(k)) is int and usage[k] >= 0}
            except gemini._CallError as error:
                record.update(status=error.status, http_status=error.http_status)
            stopped = record['status'] != 'success'
        states[record['status']] += 1
        base.write_json(output/'results'/(row['case_id']+'.json'), record)
    summary = {'mode': 'gemini', 'network_calls': calls, 'states': dict(states),
               'selected_count': len(rows), 'stopped': stopped}
    base.write_json(output/'summary.json', summary)
    return 2 if stopped else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--case-id', help='Select exactly one case, e.g. P12')
    parser.add_argument('--model', default=DEFAULT_MODEL)
    parser.add_argument('--interval', type=float, default=13)
    parser.add_argument('--fit-patches', action='store_true', help='Omit whole patches only when over the character budget')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--env-file', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.execute != (args.output is not None):
            raise ValueError('Execute and new output directory required together')
        if args.output is not None and args.output.exists():
            raise ValueError('Output already exists')
        rows, total, prompt, settings = load(args.inputs, args.case_id,
                                           model=args.model, interval=args.interval, fit_patches=args.fit_patches)
        if not args.execute:
            print(json.dumps({'mode': 'validated', 'selected_count': len(rows), 'source_total': total,
                              'network_calls': 0, 'planned_calls': len(rows), 'credentials_required': False,
                              'reduced_inputs': reduced_inputs(rows, prompt)}))
            return 0
        if args.env_file:
            if not args.env_file.is_file():
                raise ValueError('Missing environment file')
            from .config import load_env_file
            load_env_file(args.env_file)
        key = os.environ.get('GEMINI_API_KEY', '')
        gemini.GeminiClient(key, settings)  # Validate credentials before creating files.
        code = run(rows, total, prompt, settings, args.output, key, args.interval)
        summary = json.loads((args.output/'summary.json').read_text())
        print(json.dumps({**summary, 'output': str(args.output)}))
        return code
    except LengthError as error:
        print(json.dumps(error.report), file=sys.stderr)
        return 2
    except (ValueError, OSError, RecursionError):
        print('PR summary failed; check inputs, new output path and GEMINI_API_KEY configuration.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
