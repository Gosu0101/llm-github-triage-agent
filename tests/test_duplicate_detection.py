from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from triage_agent import feature_report, gemini, review_bundle


class DuplicateDetectionTests(TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.inputs = self.root / 'issues.json'
        self.rows = [
            self.row('owner/repo', 40, 'OAuth state mismatch', 'Callback rejects the OAuth state.'),
            self.row('owner/repo', 10, 'OAuth state mismatch', 'Same callback failure.'),
            self.row('owner/repo', 20, 'OAuth timeout', 'The authorization request times out.'),
            self.row('owner/repo', 30, 'Theme color', 'Dark mode font.'),
            self.row('owner/repo', 50, 'OAuth state mismatch', 'Callback rejects the OAuth state.'),
            self.row('elsewhere/repo', 5, 'OAuth state mismatch', 'Callback rejects the OAuth state.'),
        ]
        self.save()

    def row(self, repo, number, title, body):
        value = {'title': title, 'body': body}
        return {'case_id': f'I{number}', 'item_id': f'{repo}#{number}', 'origin': 'test',
                'input': value, 'input_sha256': review_bundle.input_hash(value)}

    def save(self):
        self.inputs.write_text(json.dumps({'schema_version': 'priority-preparation-inputs-v0',
                                           'cases': self.rows}), encoding='utf-8')

    def test_validation_ranks_only_earlier_issues_in_the_target_repository(self):
        args = [sys.executable, '-B', '-m', 'triage_agent.duplicate_detection',
                '--inputs', str(self.inputs), '--target', 'owner/repo#40']
        result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual([pair['candidate_item_id'] for pair in plan['pairs']],
                         ['owner/repo#10', 'owner/repo#20'])
        self.assertEqual(plan['network_calls'], 0)
        self.assertEqual(list(self.root.iterdir()), [self.inputs])

    def execute(self, response, *extra):
        from triage_agent import duplicate_detection

        output = self.root / 'run'
        args = ['--inputs', str(self.inputs), '--target', 'owner/repo#40', '--interval', '0',
                '--execute', '--output', str(output), *extra]
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'fake-duplicate-key'}), \
                patch.object(gemini, '_request', return_value=response) as request, \
                redirect_stdout(io.StringIO()):
            status = duplicate_detection.main(args)
        return status, output, request

    def response(self, number):
        prediction = {'target_issue_number': 40, 'candidate_issue_number': number,
                      'verdict': 'related', 'confidence': 0.8,
                      'evidence': ['Both describe OAuth state handling.'],
                      'rationale': 'The reports identify different symptoms.'}
        return {'status': 'completed', 'model': 'gemini-3.6-flash', 'steps': [
            {'type': 'model_output', 'content': [{'type': 'text', 'text': json.dumps(prediction)}]}]}

    def test_selected_pair_connects_to_existing_report(self):
        code, output, request = self.execute(self.response(10), '--candidate', 'owner/repo#10')
        self.assertEqual(code, 0)
        payload = request.call_args.args[0]
        self.assertEqual(json.loads(payload['input']), {
            'target_issue': {'number': 40, **self.rows[0]['input']},
            'candidate_issue': {'number': 10, **self.rows[1]['input']}})
        self.assertFalse(payload['store'])
        report = feature_report.build(issue_path=self.inputs, duplicates=output/'results')
        result = report['items'][0]['features']['duplicate']['results'][0]
        self.assertEqual(result['candidate_item_id'], 'owner/repo#10')
        self.assertEqual(result['prediction']['verdict'], 'related')

    def test_mismatched_prediction_stops_and_saves_unattempted_candidates(self):
        code, output, request = self.execute(self.response(20))
        self.assertEqual(code, 2)
        self.assertEqual(request.call_count, 1)
        summary = json.loads((output/'summary.json').read_text(encoding='utf-8'))
        self.assertEqual(summary['states'], {'invalid_schema': 1, 'unattempted': 1})
        report = feature_report.build(issue_path=self.inputs, duplicates=output/'results')
        results = report['items'][0]['features']['duplicate']['results']
        self.assertEqual([result['status'] for result in results], ['invalid_schema', 'unattempted'])
        self.assertTrue(all(result['prediction'] is None for result in results))
