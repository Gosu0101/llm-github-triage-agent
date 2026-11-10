"""Summarize saved PRs through local Ollama; offline validation by default."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sys
import time

from . import feature_report, pr_summary, review_bundle as base, review_local

DEFAULT_MODEL = 'hf.co/Jiunsong/SuperGemma-4-12b-abliterated-gguf-4bit:Q4_K_M'
SERVER = 'http://127.0.0.1:11434'


def extract(response):
    if response.get('done') is not True or response.get('done_reason') != 'stop':
        return 'incomplete_response', None
    message = response.get('message')
    if not isinstance(message, dict) or not isinstance(message.get('content'), str):
        return 'invalid_response', None
    try:
        prediction = review_local.strict_json(message['content'])
    except (ValueError, RecursionError):
        return 'invalid_json', None
    if not feature_report.summary_prediction(prediction):
        return 'invalid_schema', None
    return 'success', prediction


def run(rows, source_total, prompt, output, model, timeout, context, max_tokens):
    # Query installed weights before creating outputs; never pull or use cloud models here.
    info = review_local.request_json(SERVER+'/api/show', {'model': model}, min(timeout, 30))
    if info.get('remote_host') or info.get('remote_model'):
        raise ValueError('Cloud model is not a local experiment')
    details = info.get('details')
    if not isinstance(details, dict):
        raise ValueError('Missing local model details')
    options = {'num_ctx': context, 'num_predict': max_tokens, 'temperature': 0}
    metadata = {'mode': 'ollama-local', 'provider': 'ollama-local', 'model_requested': model,
                'prompt_version': pr_summary.PROMPT_VERSION, 'prompt_sha256': base.digest(prompt.encode())}
    output = Path(output); output.mkdir(parents=True, exist_ok=False)
    (output/'results').mkdir()
    base.write_json(output/'run-manifest.json', {**metadata,
        'started_at': datetime.now(timezone.utc).isoformat(), 'base_url': SERVER,
        'source_total': source_total, 'selected_case_ids': [r['case_id'] for r in rows],
        'options': options, 'think': False, 'timeout_seconds': timeout, 'automatic_retries': 0,
        'model_details': {k: details[k] for k in ('family', 'parameter_size', 'quantization_level') if k in details},
        'context_token_check': 'Character preflight only; inspect actual prompt_eval_count.',
        'reduced_inputs': pr_summary.reduced_inputs(rows, prompt),
        'scope': 'Local development summary; not an evaluation or cloud model run.'})
    (output/'prompt.txt').write_text(prompt, encoding='utf-8')
    states, calls, stopped = Counter(), 0, False
    for row in rows:
        record = {**metadata, **{k: row[k] for k in ('case_id', 'item_id', 'input_sha256')},
                  'status': 'unattempted', 'prediction': None, 'network_calls': 0, 'external_api_calls': 0}
        if 'input_coverage' in row:
            record['input_coverage'] = row['input_coverage']
        if not stopped:
            payload = {'model': model, 'messages': [
                {'role': 'system', 'content': prompt},
                {'role': 'user', 'content': json.dumps(row['input'], ensure_ascii=False)}],
                'format': pr_summary.SCHEMA, 'stream': False, 'think': False,
                'options': options, 'keep_alive': 0}
            started = time.monotonic(); calls += 1; record['network_calls'] = 1
            try:
                response = review_local.request_json(SERVER+'/api/chat', payload, timeout)
                record['status'], record['prediction'] = extract(response)
                if isinstance(response.get('model'), str): record['model_returned'] = response['model']
                record['usage'] = {k: response[k] for k in ('prompt_eval_count', 'eval_count')
                                   if type(response.get(k)) is int and response[k] >= 0}
                record['context_length_requested'] = context
            except review_local.LocalError as error:
                record.update(status=error.status, http_status=error.http_status)
            record['latency_seconds'] = round(time.monotonic()-started, 3)
            stopped = record['status'] != 'success'
        states[record['status']] += 1
        base.write_json(output/'results'/(row['case_id']+'.json'), record)
    summary = {'mode': 'ollama-local', 'states': dict(states), 'selected_count': len(rows),
               'network_calls': calls, 'external_api_calls': 0, 'stopped': stopped}
    base.write_json(output/'summary.json', summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--case-id')
    parser.add_argument('--fit-patches', action='store_true', help='Omit whole patches only when over the character budget')
    parser.add_argument('--model', default=DEFAULT_MODEL)
    parser.add_argument('--context', type=int, default=8192)
    parser.add_argument('--max-tokens', type=int, default=2048)
    parser.add_argument('--timeout', type=float, default=600)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.execute != (args.output is not None) or (args.output is not None and args.output.exists()):
            raise ValueError('Execute requires a new output folder')
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/:+-]{0,249}', args.model)
                or args.model.endswith(':cloud') or '://' in args.model
                or not 1024 <= args.context <= 32768 or not 1 <= args.max_tokens < args.context
                or not math.isfinite(args.timeout) or not 0 < args.timeout <= 600):
            raise ValueError('Invalid local runtime settings')
        rows, total, prompt, _ = pr_summary.load(args.inputs, args.case_id, fit_patches=args.fit_patches)
        if not args.execute:
            print(json.dumps({'mode': 'validated', 'provider': 'ollama-local', 'selected_count': len(rows),
                'planned_calls': len(rows), 'network_calls': 0, 'external_api_calls': 0,
                'credentials_required': False, 'context': args.context, 'max_tokens': args.max_tokens,
                'reduced_inputs': pr_summary.reduced_inputs(rows, prompt)}))
            return 0
        summary = run(rows, total, prompt, args.output, args.model, args.timeout, args.context, args.max_tokens)
        print(json.dumps({**summary, 'output': str(args.output)}))
        return 2 if summary['stopped'] else 0
    except pr_summary.LengthError as error:
        print(json.dumps(error.report), file=sys.stderr); return 2
    except review_local.LocalError as error:
        print(json.dumps({'mode': 'rejected', 'reason': error.status, 'http_status': error.http_status,
                          'hint': 'Check ollama serve and ollama list; model must be downloaded locally.'}), file=sys.stderr)
        return 2
    except (ValueError, OSError, RecursionError):
        print('Local PR summary failed; check inputs, local model settings and a new output folder.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
