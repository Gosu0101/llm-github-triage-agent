"""Run frozen dev classification experiments; offline unless --execute is supplied."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import time
import uuid

from .config import load_env_file
from . import review_bundle, review_local


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, default=Path('data/classification-review'),
                        help='Frozen data bundle (default: data/classification-review under the current project directory)')
    parser.add_argument('--provider', choices=('gemini', 'local'), default='gemini')
    parser.add_argument('--execute', action='store_true', help='Explicitly enable model requests')
    parser.add_argument('--env-file', type=Path, help='Optional env file; Gemini execution only')
    parser.add_argument('--output', type=Path, help='New result directory; existing directories are refused')
    parser.add_argument('--smoke', action='store_true', help='Local only: first dev item with v1')
    parser.add_argument('--base-url', default='http://127.0.0.1:1234/v1')
    parser.add_argument('--model', default='supergemma4-26b-uncensored-v2', help='Local model ID; Gemini uses frozen bundle settings')
    parser.add_argument('--timeout', type=int, default=300, help='Local request timeout')
    parser.add_argument('--max-tokens', type=int, default=2048, help='Local generation cap')
    parser.add_argument('--interval', type=float, default=13, help='Gemini: minimum seconds between transport attempts, including retries')
    args = parser.parse_args(argv)
    try:
        if not math.isfinite(args.interval) or not 0 <= args.interval <= 60:
            raise ValueError('interval must be 0..60 seconds')
        if args.smoke and args.provider != 'local':
            raise ValueError('--smoke is supported only with --provider local')
        client = None
        if args.provider == 'local':
            if not 1 <= args.timeout <= 1800 or not 128 <= args.max_tokens <= 8192:
                raise ValueError('timeout must be 1..1800; max-tokens must be 128..8192')
            client = review_local.LocalClient(args.base_url, args.model, args.timeout, args.max_tokens)
        bundle = review_bundle.load_bundle(args.bundle)
        if not args.execute:
            print(json.dumps({'mode': 'validated', 'provider': args.provider,
                'dev_items': len(bundle['dev']), 'reserve_items': len(bundle['reserve']),
                'planned_calls': 1 if args.smoke else 33 if args.provider == 'local' else 18,
                'network_calls': 0, 'credentials_required': False,
                'prompt_version': bundle['manifest']['prompt_version']}))
            return 0
        output = args.output or Path('data/llm/review') / (
            args.provider + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8])
        if output.exists():
            raise ValueError('Output directory already exists; choose a new directory')
        if args.provider == 'local':
            summary = review_local.execute(bundle, output, client, args.smoke)
            return 2 if summary['stopped'] else 0
        from . import gemini
        if args.env_file:
            load_env_file(args.env_file)
        key = os.environ.get('GEMINI_API_KEY', '')
        if not key.strip():
            raise ValueError('GEMINI_API_KEY is not configured; no API calls made')
        client = gemini.GeminiClient(key, gemini.GeminiSettings(**bundle['manifest']['settings']))
        request = gemini._request
        previous_start = None
        attempts = 0

        def paced_request(payload, api_key, timeout):
            nonlocal previous_start, attempts
            if previous_start is not None:
                time.sleep(max(0.0, args.interval - (time.monotonic() - previous_start)))
            previous_start = time.monotonic()
            attempts += 1
            return request(payload, api_key, timeout)

        # Single-process CLI scope: reuse the existing client's retry transport and restore it.
        gemini._request = paced_request
        try:
            records = review_bundle.run_batch(bundle, output, client, execution_kind='live')
        finally:
            gemini._request = request
        review_bundle.write_json(output / 'execution-notes.json', {
            'minimum_attempt_start_interval_seconds': args.interval,
            'includes_retries': True, 'actual_api_attempts': attempts,
            'latency_note': 'Gemini client latency may include pacing waits; not pure inference time.',
        })
        print(json.dumps({'result_directory': str(output.resolve()), 'summary': str(output / 'summary.json')}))
        return 2 if any(record['status'] != 'success' for record in records) else 0
    except FileNotFoundError as error:
        print('Required file not found: ' + str(error.filename), file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(str(error) if isinstance(error, ValueError) else 'Check bundle and output paths.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
