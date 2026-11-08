"""Local-only PR summaries preserve the existing report contract."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from unittest import TestCase
from unittest.mock import patch

import test_pr_summary as fixtures
from triage_agent import feature_report, gemini, pr_summary_ollama as local, review_local


def reply(value=None, reason='stop'):
    return {'done': True, 'done_reason': reason, 'model': local.DEFAULT_MODEL,
            'message': {'content': json.dumps(fixtures.prediction() if value is None else value)},
            'prompt_eval_count': 3000, 'eval_count': 250}


class OllamaPRTests(TestCase):
    setUp = fixtures.PRSummaryTests.setUp
    save = fixtures.PRSummaryTests.save

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = local.main(['--inputs', str(self.inputs), *args])
        return code, out.getvalue(), err.getvalue()

    def test_validation_has_no_local_or_cloud_requests_and_no_credentials(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(review_local, 'request_json') as http, \
                patch.object(gemini, '_request') as cloud:
            code, out, err = self.cli('--case-id', 'P12')
        self.assertEqual(code, 0, err); self.assertEqual(json.loads(out)['network_calls'], 0)
        http.assert_not_called(); cloud.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [self.inputs])

    def test_real_adapter_payload_and_report_round_trip(self):
        output = self.root/'local'
        original = self.inputs.read_bytes()
        info = {'details': {'family': 'gemma4', 'parameter_size': '11.9B', 'quantization_level': 'Q4_K_M'}}
        with patch.object(review_local, 'request_json', side_effect=[info, reply()]) as http, \
                patch.object(gemini, '_request') as cloud:
            code, out, err = self.cli('--case-id', 'P12', '--execute', '--output', str(output))
        self.assertEqual(code, 0, err); cloud.assert_not_called()
        payload = http.call_args_list[1].args[1]
        self.assertEqual(http.call_args_list[1].args[0], 'http://127.0.0.1:11434/api/chat')
        self.assertEqual(json.loads(payload['messages'][1]['content']), self.rows[0]['input'])
        self.assertFalse(payload['stream']); self.assertFalse(payload['think'])
        self.assertEqual(payload['options']['num_ctx'], 8192)
        record = json.loads((output/'results/P12.json').read_text())
        self.assertEqual(record['prompt_version'], 'pr-summary-v0.1')
        self.assertEqual(record['prompt_sha256'], fixtures.review_bundle.digest(payload['messages'][0]['content'].encode()))
        self.assertEqual(json.loads(out)['external_api_calls'], 0)
        report = feature_report.build(pr_path=self.inputs, pr_summaries=output/'results')
        self.assertEqual(report['states'], {'ready': 1, 'not_ready': 1})
        self.assertEqual(report['items'][0]['features']['pr_summary']['provider'], 'ollama-local')
        self.assertEqual(original, self.inputs.read_bytes())
        self.assertEqual(self.cli('--execute', '--output', str(output))[0], 2)

    def test_failure_is_saved_and_remaining_rows_are_unattempted(self):
        bad_json = reply(); bad_json['message']['content'] = '{'
        for i, (response, status) in enumerate([(reply(reason='length'), 'incomplete_response'),
                (reply({}), 'invalid_schema'), (bad_json, 'invalid_json'),
                (review_local.LocalError('network_error'), 'network_error')]):
            output = self.root/f'failure-{i}'
            with patch.object(review_local, 'request_json', side_effect=[{'details': {}}, response]) as http:
                self.assertEqual(self.cli('--execute', '--output', str(output))[0], 2)
            self.assertEqual(http.call_count, 2)
            self.assertEqual(json.loads((output/'results/P12.json').read_text())['status'], status)
            self.assertEqual(json.loads((output/'results/P11.json').read_text())['status'], 'unattempted')
            feature_report.build(pr_path=self.inputs, pr_summaries=output/'results')

    def test_cloud_models_and_bad_settings_are_rejected(self):
        for args in [('--model', 'gemma4:cloud'), ('--model', 'https://remote/model'),
                     ('--context', '0'), ('--max-tokens', '8192'), ('--timeout', 'nan')]:
            with patch.object(review_local, 'request_json') as http:
                self.assertEqual(self.cli(*args)[0], 2)
            http.assert_not_called()
        output = self.root/'cloud'
        with patch.object(review_local, 'request_json', return_value={'remote_host': 'https://remote', 'details': {}}):
            self.assertEqual(self.cli('--execute', '--output', str(output))[0], 2)
        self.assertFalse(output.exists())
