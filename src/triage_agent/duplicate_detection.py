"""Rank saved issue candidates and explicitly run Gemini duplicate judgments."""
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

from . import feature_report, gemini, priority_review, review_bundle

PROMPT_VERSION = 'duplicate-detection-v0.1'
DEFAULT_MODEL = 'gemini-3.6-flash'
VERDICTS = ('duplicate', 'related', 'not_duplicate', 'insufficient_information')
PROPERTIES = {
    'target_issue_number': {'type': 'integer', 'minimum': 1},
    'candidate_issue_number': {'type': 'integer', 'minimum': 1},
    'verdict': {'type': 'string', 'enum': list(VERDICTS)},
    'confidence': {'type': 'number', 'minimum': 0, 'maximum': 1},
    'evidence': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}},
    'rationale': {'type': 'string', 'minLength': 1},
}
SCHEMA = {'type': 'object', 'properties': PROPERTIES, 'required': list(PROPERTIES),
          'additionalProperties': False}


def issue_input(row):
    return {'number': feature_report.number(row['item_id']), **row['input']}


def terms(text):
    return set(re.findall(r'[^\W_]+', text.casefold()))


def overlap(left, right):
    return len(left & right) / max(1, len(left | right))


def select_pairs(path, target_id, candidate_id, limit):
    rows = priority_review.load(path)['rows']
    issues = {row['item_id']: issue_input(row) for row in rows}
    hashes = {row['item_id']: row['input_sha256'] for row in rows}
    if target_id not in issues:
        raise ValueError('Target issue not found')
    target = issues[target_id]
    repo = target_id.rsplit('#', 1)[0]
    title_terms = terms(target['title'])
    body_terms = title_terms | terms(target['body'])

    ranked = []
    for item_id, candidate in issues.items():
        if item_id.rsplit('#', 1)[0] != repo or candidate['number'] >= target['number']:
            continue
        if candidate_id is not None and item_id != candidate_id:
            continue
        candidate_title = terms(candidate['title'])
        score = (2 * overlap(title_terms, candidate_title)
                 + overlap(body_terms, candidate_title | terms(candidate['body']))) / 3
        if score > 0 or candidate_id is not None:
            ranked.append((score, candidate['number'], item_id))
    if candidate_id is not None and not ranked:
        raise ValueError('Candidate must be an earlier issue in the same repository')

    pairs = []
    for score, _, item_id in sorted(ranked, reverse=True)[:limit]:
        pairs.append({
            'item_id': target_id, 'input_sha256': hashes[target_id],
            'candidate_item_id': item_id, 'candidate_input_sha256': hashes[item_id],
            'retrieval_score': round(score, 6),
            'input': {'target_issue': target, 'candidate_issue': issues[item_id]},
        })
    return pairs


def judge(pair, client, prompt):
    settings = client.settings
    payload = {
        'model': settings.model, 'input': json.dumps(pair['input'], ensure_ascii=False),
        'system_instruction': prompt, 'store': False,
        'response_format': {'type': 'text', 'mime_type': 'application/json', 'schema': SCHEMA},
        'generation_config': {'temperature': 0, 'max_output_tokens': settings.max_output_tokens,
                              'thinking_level': 'low'},
    }
    try:
        response = gemini._request(payload, client._api_key, settings.timeout_seconds)
    except gemini._CallError as error:
        return {'status': error.status, 'prediction': None, 'http_status': error.http_status}
    status, prediction = gemini._decode_structured(response)
    if status == 'success':
        if (not feature_report.duplicate_prediction(prediction)
                or prediction['target_issue_number'] != pair['input']['target_issue']['number']
                or prediction['candidate_issue_number'] != pair['input']['candidate_issue']['number']):
            status, prediction = 'invalid_schema', None
    result = {'status': status, 'prediction': client._redact(prediction)}
    model = response.get('model')
    if isinstance(model, str) and re.fullmatch(r'gemini-[a-z0-9.-]+', model):
        result['model_returned'] = client._redact(model)
    usage = response.get('usage')
    if isinstance(usage, dict):
        result['usage'] = {k: usage[k] for k in gemini.USAGE_FIELDS
                           if type(usage.get(k)) is int and usage[k] >= 0}
    return result


def run(pairs, prompt, client, output, interval):
    results = output / 'results'
    output.mkdir(parents=True, exist_ok=False)
    results.mkdir()
    metadata = {'mode': 'gemini', 'provider': 'google', 'model_requested': client.settings.model,
                'prompt_version': PROMPT_VERSION, 'prompt_sha256': review_bundle.digest(prompt.encode())}
    selected = [{k: v for k, v in pair.items() if k != 'input'} for pair in pairs]
    review_bundle.write_json(output / 'run-manifest.json', {
        **metadata, 'pairs': selected, 'automatic_retries': 0, 'store': False,
        'minimum_request_interval_seconds': interval,
        'max_input_chars': client.settings.max_input_chars,
        'max_output_tokens': client.settings.max_output_tokens,
    })
    (output / 'prompt.txt').write_text(prompt, encoding='utf-8')
    states = Counter()
    calls, previous_start, stopped = 0, None, False
    for index, (pair, identity) in enumerate(zip(pairs, selected), 1):
        record = {**metadata, **identity, 'status': 'unattempted', 'prediction': None, 'network_calls': 0}
        if not stopped:
            if previous_start is not None:
                time.sleep(max(0, interval - (time.monotonic() - previous_start)))
            previous_start = time.monotonic()
            calls += 1
            record.update(judge(pair, client, prompt), network_calls=1)
            stopped = record['status'] != 'success'
        states[record['status']] += 1
        review_bundle.write_json(results / f'pair-{index:03}.json', record)
    summary = {'mode': 'gemini', 'network_calls': calls, 'states': dict(states), 'stopped': stopped}
    review_bundle.write_json(output / 'summary.json', summary)
    print(json.dumps({**summary, 'output': str(output)}))
    return 2 if stopped else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--target', required=True, help='Target in repository#number form')
    parser.add_argument('--candidate', help='Select one earlier issue by repository#number')
    parser.add_argument('--limit', type=int, default=3)
    parser.add_argument('--model', default=DEFAULT_MODEL)
    parser.add_argument('--interval', type=float, default=13)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--env-file', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.execute != (args.output is not None) or (args.output is not None and args.output.exists()):
            raise ValueError('Execute requires a new output folder')
        if not 1 <= args.limit <= 10 or not math.isfinite(args.interval) or not 0 <= args.interval <= 60:
            raise ValueError('Invalid candidate limit or request interval')
        settings = gemini.GeminiSettings(model=args.model, max_output_tokens=4096, timeout_seconds=60)
        settings.validate()
        pairs = select_pairs(args.inputs, args.target, args.candidate, args.limit)
        prompt = files('triage_agent').joinpath('prompts', PROMPT_VERSION + '.txt').read_text(encoding='utf-8')
        blocked = []
        for pair in pairs:
            size = len(prompt) + len(json.dumps(pair['input'], ensure_ascii=False))
            if size > settings.max_input_chars:
                blocked.append({'candidate_item_id': pair['candidate_item_id'], 'total_chars': size,
                                'excess_chars': size - settings.max_input_chars})
        if blocked:
            print(json.dumps({'mode': 'rejected', 'reason': 'prompt_input_too_long', 'blocked_pairs': blocked,
                              'max_input_chars': settings.max_input_chars, 'network_calls': 0}), file=sys.stderr)
            return 2
        if not args.execute or not pairs:
            print(json.dumps({'mode': 'validated' if pairs else 'no_candidates', 'network_calls': 0,
                              'planned_calls': len(pairs), 'credentials_required': False,
                              'pairs': [{k: v for k, v in pair.items() if k != 'input'} for pair in pairs]}))
            return 0
        if args.env_file:
            if not args.env_file.is_file():
                raise ValueError('Missing environment file')
            from .config import load_env_file
            load_env_file(args.env_file)
        client = gemini.GeminiClient(os.environ.get('GEMINI_API_KEY', ''), settings)
        return run(pairs, prompt, client, args.output, args.interval)
    except (ValueError, OSError, RecursionError):
        print('Duplicate detection failed; check input hashes, issue IDs, settings and output path.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
