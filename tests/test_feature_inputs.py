"""Collected data must become label-free, traceable offline input bundles."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from triage_agent import feature_inputs, feature_report, gemini, priority_review, review_bundle


class FeatureInputTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root/'collection.json'
        self.value = {'metadata': {'repository': 'microsoft/vscode'},
            'issues': [{'number': 1, 'repository': 'microsoft/vscode', 'is_pull_request': False,
                        'title': 'Issue', 'body': 'Original body', 'original_labels': ['never-model'],
                        'mapped_label': 'bug', 'html_url': 'not-model-input'}],
            'pull_requests': [{'number': 2, 'title': 'PR', 'body': 'PR body',
                'files': [{'filename': 'a.py', 'patch': '+change', 'patch_truncated': True,
                           'status': 'modified', 'additions': 1}]}]}
        self.write()

    def write(self):
        self.path.write_text(json.dumps(self.value, ensure_ascii=False), encoding='utf-8')

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), patch.object(gemini, '_request') as http:
            code = feature_inputs.main(['--collection', str(self.path), *args])
        http.assert_not_called()
        return code, out.getvalue(), err.getvalue()

    def test_default_only_validates_without_credentials_or_files(self):
        code, out, err = self.cli()
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual((result['issue_items'], result['pr_items'], result['network_calls']), (1, 1, 0))
        self.assertFalse(result['credentials_required'])
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_saved_inputs_round_trip_through_consumers_without_labels_or_source_mutation(self):
        original = self.path.read_bytes(); output = self.root/'prepared'
        code, _, err = self.cli('--output', str(output))
        self.assertEqual(code, 0, err)
        plan = priority_review.load(output/'issues.json')
        prs = feature_report.pr_inputs(output/'prs.json')
        self.assertEqual(plan['rows'][0]['input'], {'title': 'Issue', 'body': 'Original body'})
        self.assertEqual(plan['rows'][0]['item_id'], 'microsoft/vscode#1')
        self.assertEqual(plan['rows'][0]['input_sha256'], review_bundle.input_hash(plan['rows'][0]['input']))
        self.assertEqual(prs[0]['input']['files'], [
            {'filename': 'a.py', 'patch': '+change', 'patch_truncated': True}])
        report = feature_report.build(output/'issues.json', output/'prs.json')
        self.assertEqual(report['states'], {'not_ready': 2})
        self.assertIn('source_patch_truncated', report['items'][1]['warnings'])
        saved = ''.join(p.read_text(encoding='utf-8') for p in output.iterdir())
        for forbidden in ('never-model', 'mapped_label', 'original_labels', 'not-model-input'):
            self.assertNotIn(forbidden, saved)
        manifest = json.loads((output/'manifest.json').read_text())
        self.assertEqual(manifest['source_sha256'], review_bundle.digest(original))
        self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_records_are_rejected_before_creating_output(self):
        baseline = deepcopy(self.value)
        mutations = [
            lambda d: d['issues'][0].update(number=True),
            lambda d: d['issues'].append(deepcopy(d['issues'][0])),
            lambda d: d['issues'][0].update(repository='other/repo'),
            lambda d: d['issues'][0].update(is_pull_request=True),
            lambda d: d['issues'][0].update(body='x'*12001),
            lambda d: d['pull_requests'][0].update(number=1),
            lambda d: d['pull_requests'][0]['files'][0].update(patch_truncated='false'),
            lambda d: d['pull_requests'][0]['files'].append(deepcopy(d['pull_requests'][0]['files'][0])),
            lambda d: d['pull_requests'][0]['files'][0].update(patch=123),
            lambda d: d['metadata'].update(repository='bad repository'),
        ]
        for i, mutate in enumerate(mutations):
            with self.subTest(case=i):
                self.value = deepcopy(baseline); mutate(self.value); self.write()
                output = self.root/f'invalid-{i}'
                self.assertEqual(self.cli('--output', str(output))[0], 2)
                self.assertFalse(output.exists())

    def test_null_body_and_missing_patch_are_explicit_and_existing_output_is_preserved(self):
        self.value['issues'][0]['body'] = None
        self.value['pull_requests'][0]['files'][0].update(patch=None, patch_truncated=False)
        self.write(); output = self.root/'prepared'
        self.assertEqual(self.cli('--output', str(output))[0], 0)
        self.assertEqual(priority_review.load(output/'issues.json')['rows'][0]['input']['body'], '')
        manifest = json.loads((output/'manifest.json').read_text())
        self.assertEqual(manifest['null_bodies_normalized'], ['microsoft/vscode#1'])
        prs = feature_report.pr_inputs(output/'prs.json')
        self.assertIsNone(prs[0]['input']['files'][0]['patch'])
        snapshot = {p.name: p.read_bytes() for p in output.iterdir()}
        self.assertEqual(self.cli('--output', str(output))[0], 2)
        self.assertEqual({p.name: p.read_bytes() for p in output.iterdir()}, snapshot)

    def test_issue_only_or_pr_only_collection_does_not_emit_invalid_empty_bundle(self):
        for kind in ('issues', 'pull_requests'):
            with self.subTest(kind=kind):
                data = deepcopy(self.value); data[kind] = []
                self.path.write_text(json.dumps(data), encoding='utf-8')
                output = self.root/kind
                code, _, err = self.cli('--output', str(output))
                self.assertEqual(code, 0, err)
                self.assertEqual((output/'issues.json').exists(), kind != 'issues')
                self.assertEqual((output/'prs.json').exists(), kind != 'pull_requests')
