"""Preview or publish one saved triage result to the team's GitHub demo repository."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote

from . import feature_report, priority_review, review_bundle
from .github import GitHubClient, GitHubRequestError

DEMO_REPOSITORY = 'Gosu0101/llm-github-triage-demo'
COMMENT_MARKER = '<!-- stepup-triage-result:v1 -->'
TYPE_LABELS = {
    'bug': ('triage:type:bug', 'd73a4a'),
    'feature-request': ('triage:type:feature-request', 'a2eeef'),
    'question': ('triage:type:question', 'd876e3'),
}
PRIORITY_LABELS = {
    'high': ('triage:priority:high', 'b60205'),
    'medium': ('triage:priority:medium', 'fbca04'),
    'low': ('triage:priority:low', '0e8a16'),
}


def make_plan(report, kind, number):
    """Select the exact event target and turn its joined result into a post."""
    if report.get('schema_version') != 'triage-feature-report-v0':
        raise ValueError('Expected a feature_report report.json')
    item_id = f'{DEMO_REPOSITORY}#{number}'
    matches = [item for item in report['items'] if item['item_id'].casefold() == item_id.casefold()]
    if len(matches) != 1 or matches[0]['kind'] != kind:
        raise ValueError(f'Expected exactly one {kind} result for {item_id}')
    item = matches[0]
    if item['status'] == 'not_ready':
        raise ValueError('Analysis has no result to publish')

    features = item['features']
    records = [features['classification'], features['priority'], features['pr_summary'],
               *features['duplicate'].get('results', [])]
    mock = any(record.get('mode') == 'mock' or record.get('provider') == 'mock' for record in records)
    managed, wanted = set(), {}
    if kind == 'issue':
        for stage, field, labels in (('classification', 'type', TYPE_LABELS),
                                      ('priority', 'priority', PRIORITY_LABELS)):
            record = features[stage]
            if record['status'] in {'success', 'not_applicable', 'classification_review'}:
                managed.update(label for label, _ in labels.values())
            if record['status'] == 'success' and not record['prediction']['needs_review']:
                label, color = labels[record['prediction'][field]]
                wanted[label] = color

    markdown = feature_report.render_markdown({'items': [item]})
    body = COMMENT_MARKER + '\n' + markdown.replace('@', '＠')
    if len(body.encode('utf-8')) > 65_536:
        raise ValueError('Comment exceeds the configured 64 KiB GitHub comment budget')
    return {'target': item_id, 'kind': kind, 'number': number, 'input_sha256': item['input_sha256'],
            'mock': mock, 'body': body, 'managed_labels': sorted(managed), 'labels': wanted}


def list_records(client, path):
    page = 1
    while True:
        records = client.get(path, params={'per_page': 100, 'page': page}).data
        if not isinstance(records, list):
            raise ValueError(f'Unexpected GitHub list response for {path}')
        yield from records
        if len(records) < 100:
            return
        page += 1


def reconcile_labels(client, root, issue_path, issue, plan):
    current = {label['name'] for label in issue['labels']}
    wanted = set(plan['labels'])
    additions = wanted - current
    if additions:
        existing = {label['name'] for label in list_records(client, f'{root}/labels')}
        for name in sorted(additions - existing):
            try:
                client.write('POST', f'{root}/labels', {'name': name, 'color': plan['labels'][name]})
            except GitHubRequestError:
                # Another issue may have created this repository label at the same time.
                if name not in {label['name'] for label in list_records(client, f'{root}/labels')}:
                    raise
        updated = client.write('POST', f'{issue_path}/labels', {'labels': sorted(additions)}).data
        if not wanted.issubset(label['name'] for label in updated):
            raise ValueError('GitHub did not apply the requested labels')
    for name in sorted((current & set(plan['managed_labels'])) - wanted):
        client.write('DELETE', f'{issue_path}/labels/{quote(name, safe="")}')


def publish(client, plan, login, *, pr_head_sha=None):
    if plan['mock']:
        raise ValueError('Mock results can only be previewed')
    root = f'/repos/{DEMO_REPOSITORY}'
    number = plan['number']
    issue_path = f'{root}/issues/{number}'

    if plan['kind'] == 'issue':
        issue = client.get(issue_path).data
        if 'pull_request' in issue:
            raise ValueError('Target is a PR, not an issue')
        current = {'title': issue['title'], 'body': issue['body'] or ''}
        if review_bundle.input_hash(current) != plan['input_sha256']:
            raise ValueError('Issue content changed since analysis. Regenerate the report')
        reconcile_labels(client, root, issue_path, issue, plan)
    else:
        if not pr_head_sha or not re.fullmatch(r'[0-9a-fA-F]{40}', pr_head_sha):
            raise ValueError('PR publishing requires the commit SHA used for analysis')
        pr = client.get(f'{root}/pulls/{number}').data
        if pr['head'].get('sha', '').casefold() != pr_head_sha.casefold():
            raise ValueError('PR head changed since analysis. Regenerate the report')
        for side in ('head', 'base'):
            repository = pr[side].get('repo') or {}
            if repository.get('full_name', '').casefold() != DEMO_REPOSITORY.casefold():
                raise ValueError('PR publishing requires two branches in the demo repository')

    comments_path = f'{issue_path}/comments'
    owned = [comment for comment in list_records(client, comments_path)
             if ((comment.get('user') or {}).get('login', '').casefold() == login.casefold()
                 and (comment.get('body') or '').split('\n', 1)[0] == COMMENT_MARKER)]
    existing = min(owned, key=lambda comment: comment['id']) if owned else None
    if existing is None:
        comment = client.write('POST', comments_path, {'body': plan['body']}).data
        action = 'created'
    elif existing['body'] == plan['body']:
        comment, action = existing, 'unchanged'
    else:
        path = f"{root}/issues/comments/{existing['id']}"
        comment = client.write('PATCH', path, {'body': plan['body']}).data
        action = 'updated'
    return {'target': plan['target'], 'labels': sorted(plan['labels']),
            'comment_action': action, 'comment_url': comment['html_url']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True, help='report.json from feature_report')
    parser.add_argument('--kind', choices=('issue', 'pull_request'), required=True)
    parser.add_argument('--number', type=int, required=True)
    parser.add_argument('--pr-head-sha', help='Commit analyzed in the report; required for PR publishing')
    parser.add_argument('--publish', action='store_true', help='Write to the demo repository')
    args = parser.parse_args(argv)
    try:
        plan = make_plan(priority_review.read_json(args.report), args.kind, args.number)
        if not args.publish:
            print(json.dumps({'mode': 'preview', **plan}, ensure_ascii=False))
            return 0
        if plan['mock']:
            raise ValueError('Mock results can only be previewed')
        client = GitHubClient(os.environ.get('GITHUB_TOKEN', ''))
        # The Actions installation token does not support GET /user.
        login = ('github-actions[bot]' if os.environ.get('GITHUB_ACTIONS') == 'true'
                 else client.authenticated_user().data['login'])
        result = publish(client, plan, login, pr_head_sha=args.pr_head_sha)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (GitHubRequestError, OSError, ValueError, KeyError, TypeError) as error:
        message = str(error)
        if token := os.environ.get('GITHUB_TOKEN'):
            message = message.replace(token, '[REDACTED]')
        print(f'GitHub publish failed: {message}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
