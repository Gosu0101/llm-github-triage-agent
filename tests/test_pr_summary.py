"""PR summary CLI and saved-report integration, without a live API."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from triage_agent import feature_report, gemini, pr_summary, review_bundle


def prediction():
    return {'summary': '인증 처리 변경', 'key_changes': ['입력 검증'], 'affected_areas': ['auth'],
            'tests': [], 'risks': [], 'breaking_changes': [], 'insufficient_information': False}


def response(value=None):
    return {'status': 'completed', 'model': 'gemini-3.6-flash', 'steps': [
        {'type': 'model_output', 'content': [{'type': 'text', 'text': json.dumps(
            prediction() if value is None else value)}]}]}


class PRSummaryTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.inputs = self.root/'inputs.json'
        self.rows = []
        for number in (12, 11):
            value = {'number': number, 'title': 'Change', 'body': 'Saved source', 'files': [
                {'filename': 'a.py', 'patch': '+validate()', 'patch_truncated': False}]}
            self.rows.append({'case_id': f'P{number}', 'item_id': f'owner/repo#{number}',
                              'origin': 'test', 'input': value,
                              'input_sha256': review_bundle.input_hash(value)})
        self.save()

    def save(self):
        for row in self.rows:
            row['input_sha256'] = review_bundle.input_hash(row['input'])
        self.inputs.write_text(json.dumps({'schema_version': 'pr-summary-inputs-v0',
                                          'cases': self.rows}), encoding='utf-8')

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = pr_summary.main(['--inputs', str(self.inputs), '--interval', '0', *args])
        return code, out.getvalue(), err.getvalue()

    def execute(self, output, *args):
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'test-summary-key'}):
            return self.cli('--execute', '--output', str(output), *args)

    def test_offline_selection_and_length_rejection_precede_env_and_http(self):
        self.rows[1]['input']['body'] = 'x'*12000; self.save()
        original = self.inputs.read_bytes()
        with patch.dict(os.environ, {}, clear=True), patch.object(gemini, '_request') as http, \
                patch('triage_agent.config.load_env_file') as env:
            code, out, err = self.cli('--case-id', 'P12', '--env-file', 'missing.env')
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)['planned_calls'], 1)
            output = self.root/'blocked'
            code, out, err = self.cli('--execute', '--output', str(output), '--env-file', 'missing.env')
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)['blocked_items'][0]['case_id'], 'P11')
        self.assertFalse(output.exists()); http.assert_not_called(); env.assert_not_called()
        self.assertEqual(original, self.inputs.read_bytes())

    def test_success_sends_only_contract_fields_and_joins_existing_report(self):
        original = self.inputs.read_bytes(); output = self.root/'run'
        with patch.object(gemini, '_request', return_value=response()) as http:
            code, _, err = self.execute(output, '--case-id', 'P12')
        self.assertEqual(code, 0, err); self.assertEqual(http.call_count, 1)
        payload = http.call_args.args[0]
        self.assertEqual(payload['model'], 'gemini-3.6-flash')
        record = json.loads((output/'results/P12.json').read_text())
        self.assertEqual(record['prompt_version'], 'pr-summary-v0.1')
        self.assertEqual(record['prompt_sha256'], review_bundle.digest(payload['system_instruction'].encode()))
        self.assertEqual(json.loads(payload['input']), self.rows[0]['input'])
        self.assertFalse(payload['store'])
        report = feature_report.build(pr_path=self.inputs, pr_summaries=output/'results')
        self.assertEqual(report['states'], {'ready': 1, 'not_ready': 1})
        self.assertIn('인증 처리', feature_report.render_markdown(report))
        self.assertEqual(original, self.inputs.read_bytes())
        self.assertEqual(self.execute(output)[0], 2)

    def test_truncated_or_missing_patch_preserves_warning_and_prediction(self):
        self.rows[0]['input']['files'][0].update(patch=None, patch_truncated=True); self.save()
        output = self.root/'warnings'
        with patch.object(gemini, '_request', return_value=response()):
            self.assertEqual(self.execute(output, '--case-id', 'P12')[0], 0)
        report = feature_report.build(pr_path=self.inputs, pr_summaries=output/'results')
        item = report['items'][0]
        self.assertTrue(item['needs_review'])
        self.assertEqual(set(item['warnings']), {'source_patch_missing', 'source_patch_truncated'})
        self.assertFalse(item['features']['pr_summary']['prediction']['insufficient_information'])

    def test_failures_stop_without_retry_and_save_unattempted_records(self):
        broken_json = response(); broken_json['steps'][0]['content'][0]['text'] = '{'
        for index, (result, expected) in enumerate([
                (broken_json, 'invalid_json'), (response({}), 'invalid_schema'),
                ({'status': 'incomplete', 'steps': []}, 'incomplete_response'),
                ({'steps': []}, 'invalid_response'),
                (gemini._CallError('server_error', 503), 'server_error'),
                (gemini._CallError('rate_limited', 429), 'rate_limited')]):
            output = self.root/f'failure-{index}'
            with patch.object(gemini, '_request', side_effect=[result]) as http:
                code, out, err = self.execute(output)
                self.assertEqual(code, 2, err)
            self.assertEqual(json.loads(out)['states'], {expected: 1, 'unattempted': 1})
            self.assertTrue(json.loads(out)['stopped'])
            self.assertEqual(http.call_count, 1)
            first = json.loads((output/'results/P12.json').read_text())
            self.assertEqual(first['status'], expected); self.assertIsNone(first['prediction'])
            self.assertEqual(json.loads((output/'results/P11.json').read_text())['status'], 'unattempted')
            feature_report.build(pr_path=self.inputs, pr_summaries=output/'results')

    def test_escaped_credentials_are_redacted_after_decoding(self):
        value = prediction(); value['summary'] = 'test-summary-key'
        data = response(value)
        data['steps'][0]['content'][0]['text'] = data['steps'][0]['content'][0]['text'].replace(
            'test-summary-key', '\\u0074est-summary-key')
        output = self.root/'redacted'
        with patch.object(gemini, '_request', return_value=data):
            self.assertEqual(self.execute(output, '--case-id', 'P12')[0], 0)
        saved = json.loads((output/'results/P12.json').read_text())
        self.assertEqual(saved['prediction']['summary'], '[REDACTED]')
        for path in output.rglob('*'):
            if path.is_file(): self.assertNotIn('test-summary-key', path.read_text())

    def test_unsafe_ids_hash_mismatch_and_bad_settings_create_no_files(self):
        original = deepcopy(self.rows)
        for index, bad_id in enumerate(['../outside', 'CON', 'P12']):
            self.rows = deepcopy(original)
            self.rows[1]['case_id'] = bad_id; self.save()
            output = self.root/f'unsafe-{index}'
            with patch.object(gemini, '_request') as http:
                self.assertEqual(self.execute(output)[0], 2)
            http.assert_not_called(); self.assertFalse(output.exists())
        self.rows = original; self.save()
        for options in [('--model', 'bad'), ('--interval', 'nan'), ('--case-id', 'unknown')]:
            self.assertEqual(self.cli(*options)[0], 2)
        data = json.loads(self.inputs.read_text()); data['cases'][0]['input_sha256'] = '0'*64
        self.inputs.write_text(json.dumps(data))
        self.assertEqual(self.cli()[0], 2)

    def test_pacing_is_applied_between_attempts(self):
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'fake'}), \
                patch.object(gemini, '_request', return_value=response()) as http, \
                patch.object(pr_summary.time, 'sleep') as sleep:
            code, _, err = self.cli('--execute', '--output', str(self.root/'paced'), '--interval', '13')
        self.assertEqual(code, 0, err); self.assertEqual(http.call_count, 2)
        self.assertEqual(sleep.call_count, 1)
        self.assertGreater(sleep.call_args.args[0], 0)
