from copy import deepcopy
from unittest import TestCase
from unittest.mock import Mock, call

from triage_agent import publish, review_bundle
from triage_agent.github import GitHubResponse


class PublishTests(TestCase):
    root = f'/repos/{publish.DEMO_REPOSITORY}'
    issue = root + '/issues/8'

    def setUp(self):
        self.source = {'title': 'Login error', 'body': 'Callback rejects valid state'}
        self.item = {
            'case_id': 'I8', 'item_id': publish.DEMO_REPOSITORY + '#8',
            'input_sha256': review_bundle.input_hash(self.source), 'kind': 'issue',
            'status': 'ready', 'warnings': [],
            'features': {
                'classification': {'status': 'success', 'prediction': {
                    'type': 'bug', 'needs_review': False, 'rationale': 'Callback failure'}},
                'priority': {'status': 'success', 'prediction': {
                    'priority': 'high', 'needs_review': False, 'rationale': 'Sign-in blocked'}},
                'duplicate': {'status': 'success', 'results': [{
                    'candidate_item_id': publish.DEMO_REPOSITORY + '#3', 'status': 'success',
                    'prediction': {'verdict': 'duplicate', 'rationale': 'Same callback error'}}]},
                'pr_summary': {'status': 'not_applicable', 'prediction': None},
            },
        }
        self.report = {'schema_version': 'triage-feature-report-v0', 'items': [self.item]}

    def test_issue_creates_then_reuses_and_updates_only_its_own_comment(self):
        plan = publish.make_plan(self.report, 'issue', 8)
        bot = {'login': 'github-actions[bot]'}
        impostor = {'id': 1, 'user': {'login': 'other'}, 'body': plan['body']}
        url = 'https://github.com/Gosu0101/llm-github-triage-demo/issues/8#issuecomment-2'
        created = {'id': 2, 'user': bot, 'body': plan['body'], 'html_url': url}
        updated = {**created, 'body': plan['body'] + '\nnew result'}
        new_labels = [{'name': name} for name in plan['labels']]
        client = Mock()
        client.get.side_effect = [GitHubResponse(data, {}) for data in (
            {**self.source, 'labels': [{'name': 'manual'}, {'name': 'triage:priority:low'}]},
            new_labels, [impostor], {**self.source, 'labels': new_labels},
            [impostor, created], {**self.source, 'labels': new_labels}, [impostor, created],
        )]
        client.write.side_effect = [GitHubResponse(data, {}) for data in
                                    (new_labels, None, created, updated)]

        for expected in ('created', 'unchanged', 'updated'):
            current = {**plan, 'body': updated['body']} if expected == 'updated' else plan
            result = publish.publish(client, current, bot['login'])
            self.assertEqual((result['comment_action'], result['comment_url']), (expected, url))
        self.assertEqual(client.write.call_args_list, [
            call('POST', self.issue + '/labels', {'labels': sorted(plan['labels'])}),
            call('DELETE', self.issue + '/labels/triage%3Apriority%3Alow'),
            call('POST', self.issue + '/comments', {'body': plan['body']}),
            call('PATCH', self.root + '/issues/comments/2', {'body': updated['body']}),
        ])

    def test_pr_preserves_omitted_diff_and_posts_no_labels(self):
        item = deepcopy(self.item)
        item.update(kind='pull_request', status='needs_review', warnings=['summary_patch_omitted'])
        for name in ('classification', 'priority', 'duplicate'):
            item['features'][name] = {'status': 'not_applicable', 'prediction': None}
        item['features']['pr_summary'] = {
            'status': 'success', 'input_coverage': {'omitted_patch_files': ['big.txt']},
            'prediction': {'summary': '@someone updated settings.', 'key_changes': [],
                           'affected_areas': [], 'tests': [], 'risks': [], 'breaking_changes': [],
                           'insufficient_information': False},
        }
        plan = publish.make_plan({**self.report, 'items': [item]}, 'pull_request', 8)
        self.assertIn('요약 입력에서 제외한 diff: big.txt', plan['body'])
        self.assertNotIn('@someone', plan['body'])
        client = Mock()
        ref = {'repo': {'full_name': publish.DEMO_REPOSITORY}, 'sha': 'b' * 40}
        client.get.side_effect = [GitHubResponse({'head': ref, 'base': ref}, {}), GitHubResponse([], {})]
        client.write.return_value = GitHubResponse({'html_url': 'comment-url'}, {})
        publish.publish(client, plan, 'github-actions[bot]', pr_head_sha=ref['sha'])
        client.write.assert_called_once_with('POST', self.issue + '/comments', {'body': plan['body']})

    def test_changed_issue_and_mock_results_fail_before_write(self):
        client = Mock()
        client.get.return_value = GitHubResponse({**self.source, 'body': 'Edited'}, {})
        plan = publish.make_plan(self.report, 'issue', 8)
        with self.assertRaisesRegex(ValueError, 'changed'):
            publish.publish(client, plan, 'github-actions[bot]')
        self.item['features']['priority']['mode'] = 'mock'
        with self.assertRaisesRegex(ValueError, 'Mock'):
            publish.publish(client, publish.make_plan(self.report, 'issue', 8), 'github-actions[bot]')
        client.write.assert_not_called()

    def test_stale_pr_head_fails_before_write(self):
        client = Mock()
        client.get.return_value = GitHubResponse({'head': {'sha': 'b' * 40}}, {})
        plan = {'mock': False, 'kind': 'pull_request', 'number': 8}
        with self.assertRaisesRegex(ValueError, 'PR head changed'):
            publish.publish(client, plan, 'github-actions[bot]', pr_head_sha='a' * 40)
        client.write.assert_not_called()
