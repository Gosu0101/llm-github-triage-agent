"""Offline, hash-checked join of saved issue and PR feature results."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re
import sys

from . import priority_contract, priority_review, review_bundle as base, review_local

FAILURES = priority_review.FAILURES


def strings(value):
    return isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)


def number(item_id):
    match = re.fullmatch(r'[^/#\s]+/[^/#\s]+#([1-9][0-9]*)', item_id)
    if not match:
        raise ValueError('Expected repository#number identity')
    return int(match[1])


def pr_inputs(path):
    package = priority_review.read_json(path)
    if (not isinstance(package, dict) or package.get('schema_version') != 'pr-summary-inputs-v0'
            or not isinstance(package.get('cases'), list) or not package['cases']):
        raise ValueError('Expected PR input cases')
    rows, seen = [], set()
    for row in package['cases']:
        if not isinstance(row, dict) or set(row) != {'case_id', 'item_id', 'origin', 'input', 'input_sha256'}:
            raise ValueError('Invalid PR input wrapper')
        if not all(isinstance(row[k], str) and row[k].strip() for k in ('case_id', 'item_id', 'origin')):
            raise ValueError('Invalid PR identity')
        value = row['input']
        if (not isinstance(value, dict) or set(value) != {'number', 'title', 'body', 'files'}
                or type(value['number']) is not int or value['number'] != number(row['item_id'])
                or not all(isinstance(value[k], str) for k in ('title', 'body'))
                or not isinstance(value['files'], list) or row['item_id'] in seen):
            raise ValueError('Invalid PR input or duplicate identity')
        names = set()
        for file in value['files']:
            if (not isinstance(file, dict) or set(file) != {'filename', 'patch', 'patch_truncated'}
                    or not isinstance(file['filename'], str) or not file['filename'].strip()
                    or file['filename'] in names or type(file['patch_truncated']) is not bool
                    or not isinstance(file['patch'], (str, type(None)))):
                raise ValueError('Invalid PR file input')
            names.add(file['filename'])
        canonical = {k: value[k] for k in ('number', 'title', 'body', 'files')}
        canonical['files'] = [{k: f[k] for k in ('filename', 'patch', 'patch_truncated')} for f in value['files']]
        if row['input_sha256'] != base.input_hash(canonical):
            raise ValueError('PR input hash mismatch')
        seen.add(row['item_id']); rows.append(dict(row, input=canonical))
    return rows


def read_results(path, sources):
    if path is None:
        return []
    path = Path(path)
    paths = sorted(path.glob('*.json')) if path.is_dir() else [path]
    records = []
    for p in paths:
        value = priority_review.read_json(p)
        sources.append({'path': str(p), 'sha256': base.digest(p.read_bytes())})
        values = value if isinstance(value, list) else [value]
        for record in values:
            if (not isinstance(record, dict) or not isinstance(record.get('item_id'), str)
                    or not isinstance(record.get('input_sha256'), str)):
                raise ValueError('Invalid feature result wrapper')
            records.append(record)
    return records


def project(record, validator, extra_states=()):
    if not {'item_id', 'input_sha256', 'status', 'prediction'} <= set(record):
        raise ValueError('Missing feature result fields')
    status, prediction = record.get('status'), record.get('prediction')
    if status == 'success':
        if not validator(prediction):
            raise ValueError('Invalid successful prediction')
    elif not isinstance(status, str) or status not in FAILURES | {'unattempted', *extra_states} or prediction is not None:
        raise ValueError('Invalid feature failure or state')
    keys = ('item_id', 'input_sha256', 'status', 'prediction', 'model_requested',
            'model_returned', 'prompt_version', 'prompt_sha256')
    return {k: record[k] for k in keys if k in record}


def summary_prediction(value):
    keys = {'summary', 'key_changes', 'affected_areas', 'tests', 'risks', 'breaking_changes', 'insufficient_information'}
    return (isinstance(value, dict) and set(value) == keys
            and isinstance(value['summary'], str) and bool(value['summary'].strip())
            and all(strings(value[k]) for k in keys - {'summary', 'insufficient_information'})
            and type(value['insufficient_information']) is bool
            and (not value['insufficient_information'] or bool(value['risks'])))


def duplicate_prediction(value):
    keys = {'target_issue_number', 'candidate_issue_number', 'verdict', 'confidence', 'evidence', 'rationale'}
    return (isinstance(value, dict) and set(value) == keys
            and type(value['target_issue_number']) is int and value['target_issue_number'] > 0
            and type(value['candidate_issue_number']) is int and value['candidate_issue_number'] > 0
            and isinstance(value['verdict'], str)
            and value['verdict'] in {'duplicate', 'related', 'not_duplicate', 'insufficient_information'}
            and type(value['confidence']) in (int, float) and 0 <= value['confidence'] <= 1
            and math.isfinite(value['confidence']) and strings(value['evidence'])
            and isinstance(value['rationale'], str) and bool(value['rationale'].strip()))


def build(issue_path=None, pr_path=None, classifications=None, priorities=None,
          duplicates=None, pr_summaries=None):
    """Validate saved results, preserve their predictions, and expose missing stages."""
    if issue_path is None and pr_path is None:
        raise ValueError('At least one input bundle is required')
    issues = priority_review.load(issue_path)['rows'] if issue_path else []
    prs = pr_inputs(pr_path) if pr_path else []
    by_issue, by_pr = {r['item_id']: r for r in issues}, {r['item_id']: r for r in prs}
    if set(by_issue) & set(by_pr):
        raise ValueError('Issue and PR identities overlap')
    sources = [{'path': str(p), 'sha256': base.digest(Path(p).read_bytes())}
               for p in (issue_path, pr_path) if p is not None]
    groups, ignored = {}, Counter()
    for stage, path, rows, validator, extras in (
            ('classification', classifications, by_issue, review_local.valid_prediction, ()),
            ('priority', priorities, by_issue, priority_contract.valid_prediction,
             ('not_ready', 'not_applicable', 'classification_review', 'upstream_failed')),
            ('pr_summary', pr_summaries, by_pr, summary_prediction, ())):
        group = groups[stage] = {}
        for record in read_results(path, sources):
            item_id = record['item_id']
            if item_id not in rows:
                if stage == 'classification':
                    ignored[stage] += 1; continue
                raise ValueError('Unrelated feature identity')
            if item_id in group or record['input_sha256'] != rows[item_id]['input_sha256']:
                raise ValueError('Duplicate or mismatched feature identity')
            group[item_id] = project(record, validator, extras)
    duplicate_groups, pairs = {}, set()
    for record in read_results(duplicates, sources):
        target, candidate = record['item_id'], record.get('candidate_item_id')
        if (not isinstance(candidate, str) or target not in by_issue or candidate not in by_issue
                or target == candidate or target.split('#')[0] != candidate.split('#')[0]
                or (target, candidate) in pairs
                or record['input_sha256'] != by_issue[target]['input_sha256']
                or record.get('candidate_input_sha256') != by_issue[candidate]['input_sha256']):
            raise ValueError('Invalid duplicate pair identity or hash')
        result = project(record, duplicate_prediction)
        if result['status'] == 'success':
            prediction = result['prediction']
            if (prediction['target_issue_number'] != number(target)
                    or prediction['candidate_issue_number'] != number(candidate)):
                raise ValueError('Duplicate prediction numbers mismatch')
        result.update(candidate_item_id=candidate, candidate_input_sha256=record['candidate_input_sha256'])
        pairs.add((target, candidate)); duplicate_groups.setdefault(target, []).append(result)
    items = []
    for kind, rows in [('issue', issues), ('pull_request', prs)]:
        for row in rows:
            item_id = row['item_id']; warnings = []
            features = {s: {'status': 'not_applicable', 'prediction': None}
                        for s in ('classification', 'priority', 'duplicate', 'pr_summary')}
            review = False
            if kind == 'issue':
                cls = groups['classification'].get(item_id, {'status': 'not_ready', 'prediction': None})
                expected = ('upstream_failed' if cls['status'] in FAILURES else
                            'classification_review' if cls['status'] == 'success' and cls['prediction']['needs_review'] else
                            'not_applicable' if cls['status'] == 'success' and cls['prediction']['type'] != 'bug' else 'not_ready')
                pr = groups['priority'].get(item_id, {'status': expected, 'prediction': None})
                if expected != 'not_ready' and pr['status'] != expected:
                    raise ValueError('Priority conflicts with upstream classification')
                if (pr['status'] == 'success' or pr['status'] in FAILURES) and not (
                        cls['status'] == 'success' and cls['prediction']['type'] == 'bug' and not cls['prediction']['needs_review']):
                    raise ValueError('Priority lacks successful bug classification')
                if expected == 'not_ready' and pr['status'] in ('not_applicable', 'classification_review', 'upstream_failed'):
                    raise ValueError('Priority exclusion lacks upstream evidence')
                entries = duplicate_groups.get(item_id, [])
                dup_status = ('failed' if any(e['status'] in FAILURES for e in entries) else
                              'unattempted' if any(e['status'] == 'unattempted' for e in entries) else
                              'success' if entries else 'not_ready')
                features.update(classification=cls, priority=pr,
                                duplicate={'status': dup_status, 'results': entries})
                review = any(p['prediction'] and p['prediction']['needs_review'] for p in (cls, pr))
                review = bool(review or any(e['prediction'] and e['prediction']['verdict'] == 'insufficient_information' for e in entries))
                relevant = ('classification', 'priority', 'duplicate')
            else:
                result = groups['pr_summary'].get(item_id, {'status': 'not_ready', 'prediction': None})
                features['pr_summary'] = result
                files = row['input']['files']
                if any(f['patch_truncated'] for f in files): warnings.append('source_patch_truncated')
                if not files or any(f['patch'] is None for f in files): warnings.append('source_patch_missing')
                review = bool(result['prediction'] and (result['prediction']['insufficient_information'] or warnings))
                relevant = ('pr_summary',)
            states = [features[s]['status'] for s in relevant]
            status = ('failed' if any(s in FAILURES | {'failed', 'upstream_failed'} for s in states) else
                      'not_ready' if all(s == 'not_ready' for s in states) else
                      'partial' if any(s in {'not_ready', 'unattempted'} for s in states) else
                      'needs_review' if review else 'ready')
            items.append({**{k: row[k] for k in ('case_id', 'item_id', 'input_sha256')},
                          'kind': kind, 'status': status, 'needs_review': review,
                          'warnings': warnings, 'features': features})
    return {'schema_version': 'triage-feature-report-v0', 'network_calls': 0,
            'states': dict(Counter(i['status'] for i in items)), 'ignored_records': dict(ignored),
            'sources': sources, 'items': items,
            'scope': 'Saved outputs only; ready describes joined records, not correctness or complete candidate coverage.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('issue-inputs', 'pr-inputs', 'classifications', 'priorities', 'duplicates', 'pr-summaries', 'output'):
        parser.add_argument('--'+name, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.output is not None and args.output.exists():
            raise ValueError('Output already exists')
        report = build(args.issue_inputs, args.pr_inputs, args.classifications,
                       args.priorities, args.duplicates, args.pr_summaries)
        if args.output is not None:
            args.output.mkdir(parents=True, exist_ok=False)
            base.write_json(args.output/'report.json', report)
        print(json.dumps({'mode': 'saved' if args.output else 'validated',
                          'total': len(report['items']), 'states': report['states'],
                          'network_calls': 0, 'ignored_records': report['ignored_records']}))
        return 0
    except (OSError, ValueError, RecursionError):
        print('Feature report validation failed; check input hashes, result contracts and output path.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
