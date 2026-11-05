"""Prepare label-free feature inputs from one saved collector JSON, without network calls."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

from . import priority_contract, review_bundle as base


def prepare(path):
    original = Path(path).read_bytes()
    source = priority_contract.strict_json(original.decode('utf-8-sig'))
    if not isinstance(source, dict) or not isinstance(source.get('metadata'), dict):
        raise ValueError('Expected collector metadata')
    repository = source['metadata'].get('repository')
    if not isinstance(repository, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid source repository')
    issues, prs, seen, null_bodies = [], [], set(), []
    for key, destination, prefix in (('issues', issues, 'I'), ('pull_requests', prs, 'P')):
        records = source.get(key, [])
        if not isinstance(records, list):
            raise ValueError('Expected collector record list')
        for record in records:
            if not isinstance(record, dict) or not {'number', 'title', 'body'} <= set(record):
                raise ValueError('Missing collected fields')
            number, title, body = record['number'], record['title'], record['body']
            if (type(number) is not int or number < 1 or len(prefix+str(number)) > 64
                    or not isinstance(title, str) or not title.strip()
                    or (body is not None and not isinstance(body, str))
                    or record.get('repository', repository) != repository):
                raise ValueError('Invalid collected identity or text')
            item_id = repository+'#'+str(number)
            if item_id in seen:
                raise ValueError('Duplicate or overlapping identity')
            seen.add(item_id)
            if body is None:
                body = ''; null_bodies.append(item_id)
            value = {'title': title, 'body': body}
            if key == 'issues':
                if record.get('is_pull_request') is not False:
                    raise ValueError('Issue record must explicitly exclude PRs')
                if len(title)+len(body) > 12000:
                    raise ValueError('Issue too long; truncation is forbidden')
            else:
                if record.get('is_pull_request', True) is not True:
                    raise ValueError('Conflicting PR identity')
                files = record.get('files')
                if not isinstance(files, list):
                    raise ValueError('Missing collected PR files')
                selected, names = [], set()
                for file in files:
                    if (not isinstance(file, dict)
                            or not {'filename', 'patch', 'patch_truncated'} <= set(file)
                            or not isinstance(file['filename'], str) or not file['filename'].strip()
                            or file['filename'] in names
                            or (file['patch'] is not None and not isinstance(file['patch'], str))
                            or type(file['patch_truncated']) is not bool):
                        raise ValueError('Invalid collected PR patch')
                    names.add(file['filename'])
                    selected.append({k: file[k] for k in ('filename', 'patch', 'patch_truncated')})
                value = {'number': number, **value, 'files': selected}
            destination.append({'case_id': prefix+str(number), 'item_id': item_id,
                'origin': 'saved-collection-development', 'input': value,
                'input_sha256': base.input_hash(value)})
    if not issues and not prs:
        raise ValueError('No collected inputs')
    manifest = {'schema_version': 'triage-feature-inputs-v0', 'repository': repository,
        'source_sha256': base.digest(original), 'issue_items': len(issues), 'pr_items': len(prs),
        'null_bodies_normalized': null_bodies, 'network_calls': 0,
        'scope': 'Development preparation only; no predictions or frozen evaluation split.'}
    bundles = {}
    if issues:
        bundles['issues.json'] = {'schema_version': 'priority-preparation-inputs-v0', 'cases': issues}
    if prs:
        bundles['prs.json'] = {'schema_version': 'pr-summary-inputs-v0', 'cases': prs}
    return manifest, bundles


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collection', type=Path, required=True)
    parser.add_argument('--output', type=Path, help='New directory; omitted means validation only')
    args = parser.parse_args(argv)
    try:
        if args.output is not None and args.output.exists():
            raise ValueError('Output already exists')
        manifest, bundles = prepare(args.collection)
        if args.output is not None:
            args.output.mkdir(parents=True, exist_ok=False)
            for name, value in {**bundles, 'manifest.json': manifest}.items():
                base.write_json(args.output/name, value)
        print(json.dumps({'mode': 'saved' if args.output else 'validated',
            'issue_items': manifest['issue_items'], 'pr_items': manifest['pr_items'],
            'source_sha256': manifest['source_sha256'], 'network_calls': 0,
            'credentials_required': False}))
        return 0
    except (OSError, ValueError, RecursionError):
        print('Feature input preparation failed; check collector fields, input length and new output path.',
              file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
