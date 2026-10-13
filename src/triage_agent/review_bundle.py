"""Frozen classification bundle validation, execution records and dev comparison."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re


TYPES = ('bug', 'feature-request', 'question')
FILES = {'inputs/dev-inputs.jsonl', 'inputs/reserve-inputs.jsonl',
         'reference/dev-gold.jsonl', 'reference/v0-results.jsonl',
         'reference/run-manifest.v0.json', 'reference/classification-v0-review.txt',
         'classification-v1-review.txt'}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_rows(path):
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    ids = [row['item_id'] for row in rows]
    if len(set(ids)) != len(ids):
        raise ValueError('duplicate item_id')
    return rows


def input_hash(model_input):
    return digest(json.dumps(model_input, ensure_ascii=False).encode('utf-8'))


def load_bundle(root):
    root = Path(root)
    manifest = json.loads((root / 'preflight.json').read_text(encoding='utf-8'))
    if manifest.get('schema_version') != 1 or set(manifest['hashes']) != FILES:
        raise ValueError('unexpected bundle manifest')
    for relative, expected in manifest['hashes'].items():
        if digest((root / relative).read_bytes()) != expected:
            raise ValueError('hash mismatch: ' + relative)
    prompt = (root / 'classification-v1-review.txt').read_text(encoding='utf-8')
    if not prompt.strip():
        raise ValueError('empty prompt')
    dev = read_rows(root / 'inputs/dev-inputs.jsonl')
    reserve = read_rows(root / 'inputs/reserve-inputs.jsonl')
    if len(dev) != manifest['dev_count'] or [r['item_id'] for r in reserve] != manifest['reserve_ids']:
        raise ValueError('unexpected dataset membership')
    rows = dev + reserve
    if len({r['item_id'] for r in rows}) != len(rows):
        raise ValueError('dev/reserve overlap')
    for row in rows:
        if set(row) != {'item_id', 'input'} or not re.fullmatch(r'microsoft/vscode#[0-9]+', row['item_id']):
            raise ValueError('invalid input wrapper')
        value = row['input']
        if not isinstance(value, dict) or set(value) != {'title', 'body'}:
            raise ValueError('model input must contain exactly title/body')
        if not all(isinstance(v, str) for v in value.values()):
            raise ValueError('title/body must be strings')
        if len(prompt) + len(value['title']) + len(value['body']) > manifest['settings']['max_input_chars']:
            raise ValueError('input too long; no truncation is permitted')
    gold = read_rows(root / 'reference/dev-gold.jsonl')
    baseline = read_rows(root / 'reference/v0-results.jsonl')
    ids = {row['item_id'] for row in dev}
    if {r['item_id'] for r in gold} != ids or {r['item_id'] for r in baseline} != ids:
        raise ValueError('input/gold/baseline membership mismatch')
    if any(row['mapped_label'] not in TYPES for row in gold):
        raise ValueError('invalid gold type')
    inputs_by_id = {r['item_id']: r['input'] for r in dev}
    baseline_prompt = (root / 'reference/classification-v0-review.txt').read_text(encoding='utf-8')
    for record in baseline:
        if record['input_sha256'] != input_hash(inputs_by_id[record['item_id']]):
            raise ValueError('v0 input hash mismatch')
        if record['prompt_sha256'] != digest(baseline_prompt.encode('utf-8')):
            raise ValueError('v0 prompt hash mismatch')
    return {'root': root, 'manifest': manifest, 'prompt': prompt,
            'dev': dev, 'reserve': reserve, 'gold': gold, 'baseline': baseline}


def compare(records, gold, baseline, reserve_ids):
    current = {r['item_id']: r for r in records}
    old = {r['item_id']: r for r in baseline}
    if len(current) != len(records):
        raise ValueError('duplicate results')
    expected_ids = {r['item_id'] for r in gold} | set(reserve_ids)
    if set(current) - expected_ids:
        raise ValueError('unexpected result item')

    def prediction(record):
        if not record or record.get('status') != 'success':
            return None
        return (record.get('prediction') or {}).get('type')

    table = []
    counts = {'total': len(gold), 'attempted': 0, 'correct': 0, 'review': 0,
              'failed': 0, 'unattempted': 0, 'v0_correct': 0, 'retained_v0_correct': 0,
              'regressed_v0_correct': 0, 'improved_v0_incorrect': 0}
    for row in gold:
        item_id, target = row['item_id'], row['mapped_label']
        record, previous = current.get(item_id), old.get(item_id)
        predicted, v0 = prediction(record), prediction(previous)
        status = record['status'] if record else 'unattempted'
        correct = bool(record and status == 'success' and predicted == target)
        v0_correct = bool(previous and prediction(previous) == target)
        counts['attempted'] += bool(record)
        counts['correct'] += correct
        counts['review'] += bool(record and status == 'success' and predicted is None)
        counts['failed'] += bool(record and status != 'success')
        counts['unattempted'] += record is None
        counts['v0_correct'] += v0_correct
        counts['retained_v0_correct'] += v0_correct and correct
        counts['regressed_v0_correct'] += bool(record) and v0_correct and not correct
        counts['improved_v0_incorrect'] += bool(previous) and not v0_correct and correct
        table.append({'group': 'dev', 'item_id': item_id, 'reference_type': target,
                      'v0_type': v0, 'v1_type': predicted, 'status': status,
                      'correct': correct, 'v0_correct': v0_correct,
                      'rationale': (record.get('prediction') or {}).get('rationale', '') if record else ''})
    counts['accuracy'] = counts['correct'] / counts['total'] if counts['total'] else None
    metrics = {}
    confusion = {t: {p: 0 for p in (*TYPES, 'review', 'failed', 'unattempted')} for t in TYPES}
    for row in table:
        column = row['v1_type'] if row['status'] == 'success' and row['v1_type'] in TYPES else (
            'review' if row['status'] == 'success' else 'unattempted' if row['status'] == 'unattempted' else 'failed')
        confusion[row['reference_type']][column] += 1
    for target in TYPES:
        support = sum(row['mapped_label'] == target for row in gold)
        tp = sum(r['reference_type'] == target and r['v1_type'] == target for r in table)
        fp = sum(r['reference_type'] != target and r['v1_type'] == target for r in table)
        fn = support - tp
        metrics[target] = {'support': support, 'precision': tp / (tp + fp) if tp + fp else None,
                           'recall': tp / support if support else None,
                           'f1': 2 * tp / (2 * tp + fp + fn) if support else None}
    counts['metrics'] = metrics
    counts['macro_f1'] = (sum(m['f1'] for m in metrics.values()) / 3
                          if all(m['f1'] is not None for m in metrics.values()) else None)
    counts['confusion_matrix'] = confusion
    reserve = {'total': len(reserve_ids), 'attempted': 0, 'review': 0, 'failed': 0,
               'unattempted': 0, 'reference_question_matches': 0,
               'scope': 'Ambiguous supplementary dev cases; excluded from official 15-item metrics.'}
    for item_id in reserve_ids:
        record = current.get(item_id)
        status = record['status'] if record else 'unattempted'
        predicted = prediction(record)
        reserve['attempted'] += bool(record)
        reserve['unattempted'] += record is None
        reserve['failed'] += bool(record and status != 'success')
        reserve['review'] += bool(record and status == 'success' and predicted is None)
        reserve['reference_question_matches'] += predicted == 'question'
        table.append({'group': 'reserve', 'item_id': item_id, 'reference_type': 'question',
                      'v0_type': None, 'v1_type': predicted, 'status': status,
                      'correct': '', 'v0_correct': '',
                      'rationale': (record.get('prediction') or {}).get('rationale', '') if record else ''})
    return {'scope': 'Small dev reference results, not final evaluation.', 'dev': counts, 'reserve': reserve}, table


def write_json(path, value):
    with Path(path).open('x', encoding='utf-8') as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write('\n')


def must_stop(record):
    return (record['status'] in ('auth_error', 'rate_limited') or
            any(attempt.get('http_status') == 402 for attempt in record['attempts']))


def run_batch(package, output, client, execution_kind='test-double'):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    results_dir = output / 'results'
    results_dir.mkdir()
    manifest = package['manifest']
    write_json(output / 'run-manifest.json', {
        'started_at': datetime.now(timezone.utc).isoformat(), 'execution_kind': execution_kind,
        'prompt_version': manifest['prompt_version'], 'hashes': manifest['hashes'],
        'settings': manifest['settings'], 'retry_note': manifest['retry_note'],
        'planned_ids': [r['item_id'] for r in package['dev'] + package['reserve']]})
    records = []
    for group, rows in (('dev', package['dev']), ('reserve', package['reserve'])):
        dataset_hash = manifest['hashes'][f'inputs/{group}-inputs.jsonl']
        for row in rows:
            # Only these two fields cross the model boundary. Gold is used after execution.
            record = client.generate(title=row['input']['title'], body=row['input']['body'],
                                     instruction=package['prompt'], prompt_version=manifest['prompt_version'])
            record.update(item_id=row['item_id'], execution_group=group, execution_kind=execution_kind,
                          dataset_version=manifest['dataset_version'], dataset_sha256=dataset_hash)
            write_json(results_dir / (row['item_id'].split('#')[1] + '.json'), record)
            records.append(record)
            if must_stop(record):
                break
        if records and must_stop(records[-1]):
            break
    report, table = compare(records, package['gold'], package['baseline'], manifest['reserve_ids'])
    report.update(execution_kind=execution_kind, calls=sum(len(r['attempts']) for r in records),
                  prompt_version=manifest['prompt_version'], settings=manifest['settings'],
                  retry_note=manifest['retry_note'])
    write_json(output / 'summary.json', report)
    with (output / 'dev-comparison.csv').open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(table[0]))
        writer.writeheader()
        writer.writerows(table)
    with (output / 'attempt-history.jsonl').open('x', encoding='utf-8') as stream:
        for record in records:
            for attempt in record['attempts']:
                stream.write(json.dumps({'item_id': record['item_id'], 'group': record['execution_group'],
                                         'execution_kind': execution_kind, **attempt}, ensure_ascii=False) + '\n')
    return records


def load_results(package, directory):
    directory = Path(directory)
    run = json.loads((directory / 'run-manifest.json').read_text(encoding='utf-8'))
    if run['hashes'] != package['manifest']['hashes'] or run['settings'] != package['manifest']['settings']:
        raise ValueError('results belong to a different bundle or settings')
    expected = {r['item_id']: r for r in package['dev'] + package['reserve']}
    records = []
    for path in sorted((directory / 'results').glob('*.json')):
        record = json.loads(path.read_text(encoding='utf-8'))
        item_id = record['item_id']
        if item_id not in expected or record['input_sha256'] != input_hash(expected[item_id]['input']):
            raise ValueError('result input mismatch')
        if record['prompt_sha256'] != digest(package['prompt'].encode('utf-8')):
            raise ValueError('result prompt mismatch')
        records.append(record)
    return records, run['execution_kind']
