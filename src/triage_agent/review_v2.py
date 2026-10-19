"""Frozen v2 follow-up: 18 primary items, separate boundary stability checks."""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import time
import uuid

from . import review_bundle as base, review_local
from .config import load_env_file

FILES = {'classification-v2-review.txt', 'reference/v1-gemini-results.jsonl',
         'reference/v1-local-results.jsonl', 'local-settings.json'}
BOUNDARIES = ('microsoft/vscode#331658', 'microsoft/vscode#338087')


def load(root, provider):
    package = base.load_bundle(root)
    root = Path(root)
    manifest = json.loads((root / 'v2-preflight.json').read_text(encoding='utf-8'))
    if manifest.get('prompt_version') != 'classification-v2-review' or set(manifest['hashes']) != FILES:
        raise ValueError('Invalid v2 manifest')
    for name, expected in manifest['hashes'].items():
        if base.digest((root / name).read_bytes()) != expected:
            raise ValueError('v2 hash mismatch: ' + name)
    prompt = (root / 'classification-v2-review.txt').read_text(encoding='utf-8')
    if not prompt.strip():
        raise ValueError('Empty v2 prompt')
    inputs = {r['item_id']: r['input'] for r in package['dev'] + package['reserve']}
    baseline = base.read_rows(root / f'reference/v1-{provider}-results.jsonl')
    if {r['item_id'] for r in baseline} != set(inputs):
        raise ValueError('v1 baseline membership mismatch')
    for record in baseline:
        if (record['input_sha256'] != base.input_hash(inputs[record['item_id']])
                or record['prompt_sha256'] != base.digest(package['prompt'].encode('utf-8'))
                or record['status'] != 'success'):
            raise ValueError('Invalid v1 baseline provenance')
    for value in inputs.values():
        if len(prompt) + len(value['title']) + len(value['body']) > package['manifest']['settings']['max_input_chars']:
            raise ValueError('v2 input too long; truncation is forbidden')
    package.update(v1_prompt=package['prompt'], prompt=prompt, baseline=baseline,
                   provider=provider, v2_manifest=manifest, boundary_ids=list(BOUNDARIES),
                   local_settings=json.loads((root / 'local-settings.json').read_text(encoding='utf-8')))
    return package


def run(package, output, client, repeats=0, smoke=False):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    results = output / 'results'; results.mkdir()
    stability = output / 'stability'; stability.mkdir()
    for name, text in [('classification-v1-review.txt', package['v1_prompt']),
                       ('classification-v2-review.txt', package['prompt'])]:
        (output / name).write_text(text, encoding='utf-8', newline='\n')
    settings = asdict(client.settings) if is_dataclass(client.settings) else client.settings
    base.write_json(output / 'run-manifest.json', {
        'started_at': datetime.now(timezone.utc).isoformat(), 'provider': package['provider'],
        'prompt_version': 'classification-v2-review', 'settings': settings,
        'minimum_attempt_interval_seconds': package.get('attempt_interval'),
        'base_url': getattr(client, 'base_url', None), 'timeout_seconds': getattr(client, 'timeout', None),
        'input_bundle_hashes': package['manifest']['hashes'],
        'v2_bundle_hashes': package['v2_manifest']['hashes'],
        'primary_count': 1 if smoke else 18, 'boundary_repeats_per_item': 0 if smoke else repeats,
        'local_environment_record': package['local_settings'],
        'scope': 'Development only. Stability results never replace primary results.'})
    base.write_json(output / 'local-settings.json', package['local_settings'])
    primary, repeated, stopped = [], [], None
    rows = package['dev'][:1] if smoke else package['dev'] + package['reserve']
    dev_ids = {r['item_id'] for r in package['dev']}
    for row in rows:
        record = client.generate(**row['input'], instruction=package['prompt'],
                                 prompt_version='classification-v2-review')
        record.update(item_id=row['item_id'], execution_group='dev' if row['item_id'] in dev_ids else 'reserve')
        base.write_json(results / (row['item_id'].split('#')[1] + '.json'), record)
        primary.append(record)
        print(f"v2 {len(primary)}/{len(rows)} {row['item_id']}: {record['status']}", flush=True)
        if record['status'] != 'success':
            stopped = {'group':'primary', 'item_id':row['item_id'], 'status':record['status']}
            break
    by_id = {r['item_id']:r for r in package['dev']}
    if not stopped and not smoke:
        for item_id in package['boundary_ids'] if repeats else []:
            if item_id not in by_id:
                raise ValueError('Boundary item absent from dev')
            for number in range(1, repeats + 1):
                record = client.generate(**by_id[item_id]['input'], instruction=package['prompt'],
                                         prompt_version='classification-v2-review')
                record.update(item_id=item_id, repeat_number=number, execution_group='stability')
                base.write_json(stability / f"{item_id.split('#')[1]}-{number}.json", record)
                repeated.append(record)
                print(f"stability {item_id} {number}/{repeats}: {record['status']}", flush=True)
                if record['status'] != 'success':
                    stopped = {'group':'stability', 'item_id':item_id, 'status':record['status']}
                    break
            if stopped:
                break
    comparison, table = base.compare(primary, package['gold'], package['baseline'], package['manifest']['reserve_ids'])
    comparison['dev'] = {k.replace('v0', 'v1'):v for k,v in comparison['dev'].items()}
    comparison['reserve'].pop('reference_question_matches', None)
    for row in table:
        row['v2_type'] = row.pop('v1_type')
        row['v1_type'] = row.pop('v0_type')
        row['v1_correct'] = row.pop('v0_correct')
        if row['group'] == 'reserve':
            row['reference_type'] = 'ambiguous; excluded'
    with (output / 'v1-v2-comparison.csv').open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(table[0])); writer.writeheader(); writer.writerows(table)
    report = {'prompt_version':'classification-v2-review', 'provider':package['provider'],
              'primary_calls':len(primary), 'repeat_calls':len(repeated), 'stopped':stopped,
              'transport_attempts':sum(len(r['attempts']) for r in primary + repeated),
              'comparison':comparison, 'stability':repeated,
              'stop_tuning_note':'No further prompt tuning against these dev items after v2. Evaluate separately.'}
    base.write_json(output / 'summary.json', report)
    print('Results: ' + str(output.resolve()), flush=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, default=Path('data/classification-review-v2'))
    parser.add_argument('--provider', choices=('gemini','local'), default='gemini')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--boundary-repeats', type=int,
                        help='Extra runs per boundary item: 0..3 (default: Gemini 3, local 0)')
    parser.add_argument('--interval', type=float, default=13)
    parser.add_argument('--model', default='supergemma4-26b-uncensored-v2')
    parser.add_argument('--base-url', default='http://127.0.0.1:1234/v1')
    parser.add_argument('--max-tokens', type=int, default=2048)
    parser.add_argument('--timeout', type=int, default=300)
    args = parser.parse_args(argv)
    try:
        repeats = args.boundary_repeats if args.boundary_repeats is not None else (3 if args.provider == 'gemini' else 0)
        if not 0 <= repeats <= 3:
            raise ValueError('boundary-repeats must be 0..3')
        if not math.isfinite(args.interval) or not 0 <= args.interval <= 60:
            raise ValueError('interval must be 0..60')
        if not 128 <= args.max_tokens <= 8192 or not 1 <= args.timeout <= 1800:
            raise ValueError('Invalid local generation or timeout limit')
        package = load(args.bundle, args.provider)
        package['attempt_interval'] = args.interval if args.provider == 'gemini' else None
        if not args.execute:
            print(json.dumps({'mode':'validated','provider':args.provider,'dev_items':15,'reserve_items':3,
                              'planned_calls':1 if args.smoke else 18+2*repeats,'network_calls':0,
                              'prompt_version':'classification-v2-review'}))
            return 0
        if args.provider == 'local':
            client = review_local.LocalClient(args.base_url, args.model, args.timeout, args.max_tokens)
        else:
            from . import gemini
            if args.env_file:
                load_env_file(args.env_file)
            client = gemini.GeminiClient(os.environ.get('GEMINI_API_KEY',''), gemini.GeminiSettings(**package['manifest']['settings']))
        requested = args.model if args.provider == 'local' else client.settings.model
        known_models = {r.get('model_requested') for r in package['baseline'] if r.get('model_requested')}
        if known_models and known_models != {requested}:
            raise ValueError('Requested model differs from the same-provider v1 baseline')
        output = args.output or Path('data/llm/review') / ('v2-'+args.provider+'-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8])
        if args.provider == 'local':
            report = run(package, output, client, repeats, args.smoke)
        else:
            original = gemini._request
            previous = None
            def paced(*parameters):
                nonlocal previous
                if previous is not None:
                    time.sleep(max(0, args.interval-(time.monotonic()-previous)))
                previous = time.monotonic()
                return original(*parameters)
            with_request = gemini._request
            gemini._request = paced
            try:
                report = run(package, output, client, repeats, args.smoke)
            finally:
                gemini._request = with_request
        return 2 if report['stopped'] else 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(str(error) if isinstance(error, ValueError) else 'Check v2 bundle and output paths.', file=__import__('sys').stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
