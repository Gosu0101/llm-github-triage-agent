"""Gemini priority CLI integration; no live service or real credentials."""
import hashlib
import json
import os
from unittest import TestCase, main
from unittest.mock import patch

import test_priority_local
from triage_agent import gemini, priority_review


def interaction(priority='medium', status='completed', text=None):
    prediction = {'priority': priority, 'rationale': '본문에서 확인된 영향.',
                  'needs_review': priority is None,
                  'missing_information': ['필요한 작업을 완료할 수 있나요?'] if priority is None else []}
    return {'status': status, 'model': 'gemini-3.8-flash',
            'steps': [{'type': 'thought', 'content': []},
                      {'type': 'model_output', 'content': [
                          {'type': 'text', 'text': text if text is not None else json.dumps(prediction)}]}],
            'usage': {'total_input_tokens': 30, 'total_output_tokens': 40,
                      'total_thought_tokens': 10, 'total_tokens': 80}}


class PriorityGeminiTests(TestCase):
    def setUp(self):
        test_priority_local.PriorityLocalTests.setUp(self)
        self.env_patch = patch.dict(os.environ, {'GEMINI_API_KEY': 'fake-priority-key'})
        self.env_patch.start(); self.addCleanup(self.env_patch.stop)

    def cli(self, *options):
        return test_priority_local.PriorityLocalTests.cli(self, '--provider', 'gemini', '--interval', '0', *options)

    def test_offline_needs_no_key_env_file_or_http(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(gemini, '_request') as request:
            code, out, err = self.cli('--env-file', str(self.root/'missing.env'))
        self.assertEqual(code, 0, err)
        plan = json.loads(out)
        self.assertEqual((plan['provider'], plan['model_requested'], plan['planned_calls'],
                          plan['network_calls'], plan['credentials_required']),
                         ('google', 'gemini-3.8-flash', 2, 0, False))
        self.assertEqual(plan['prompt_version'], 'priority-bug-v0.3-draft')
        request.assert_not_called()

    def test_execute_exact_payload_hold_and_saved_snapshot(self):
        output = self.root/'gemini-all'
        with patch.object(gemini, '_request', side_effect=[interaction(None), interaction()]) as request:
            code, out, err = self.cli('--execute', '--output', str(output))
        self.assertEqual(code, 0, err)
        self.assertEqual(request.call_count, 2)
        for index, call in enumerate(request.call_args_list):
            payload, key, timeout = call.args
            self.assertEqual(json.loads(payload['input']), self.rows[index*2]['input'])
            self.assertEqual(key, 'fake-priority-key')
            self.assertEqual(timeout, 60)
            self.assertIs(payload['store'], False)
            self.assertEqual(payload['generation_config'],
                             {'temperature': 0, 'max_output_tokens': 4096, 'thinking_level': 'low'})
            self.assertEqual(list(payload['response_format']['schema']['properties']),
                             ['priority', 'rationale', 'needs_review', 'missing_information'])
            self.assertEqual(hashlib.sha256(payload['system_instruction'].encode()).hexdigest(),
                             'dd62f2444751b9b08ac91fbe4d55e9f3dbae555131cb81a3f5b6235650cfa54b')
        manifest = json.loads((output/'run-manifest.json').read_text())
        summary = json.loads((output/'summary.json').read_text())
        self.assertEqual((summary['mode'], summary['network_calls'], summary['simulated_calls'],
                          summary['success'], summary['review']), ('gemini', 2, 0, 2, 1))
        self.assertEqual(manifest['provider'], 'google')
        self.assertNotIn('context_length_reported', manifest)
        self.assertEqual((output/'priority-bug-v0.3-draft.txt').read_text(encoding='utf-8'),
                         request.call_args.args[0]['system_instruction'])
        record = json.loads((output/'results/R01.json').read_text())
        self.assertEqual(record['usage'], {'prompt_tokens': 30, 'completion_tokens': 40,
                                          'total_thought_tokens': 10, 'total_tokens': 80})
        self.assertEqual(record['input_sha256'], self.rows[0]['input_sha256'])
        self.assertNotIn('fake-priority-key', ''.join(p.read_text() for p in output.rglob('*') if p.is_file()))

    def test_incomplete_saves_usage_failure_and_stops(self):
        output = self.root/'incomplete'
        with patch.object(gemini, '_request', return_value=interaction(status='incomplete')) as request:
            code, out, err = self.cli('--execute', '--output', str(output))
        self.assertEqual(code, 2, err); self.assertEqual(request.call_count, 1)
        record = json.loads((output/'results/R01.json').read_text())
        summary = json.loads((output/'summary.json').read_text())
        self.assertEqual(record['status'], 'incomplete_response')
        self.assertEqual(record['usage']['total_tokens'], 80)
        self.assertIsNone(record['prediction'])
        self.assertEqual((summary['failed'], summary['review'], summary['unattempted']), (1, 0, 1))

    def test_429_and_network_failure_are_saved_without_retry(self):
        for status, http in [('rate_limited', 429), ('network_error', None)]:
            with self.subTest(status=status):
                output = self.root/status
                with patch.object(gemini, '_request', side_effect=gemini._CallError(status, http)) as request:
                    code, out, err = self.cli('--execute', '--output', str(output))
                self.assertEqual(code, 2, err); self.assertEqual(request.call_count, 1)
                record = json.loads((output/'results/R01.json').read_text())
                self.assertEqual((record['status'], record['attempts'][0]['http_status']), (status, http))
                self.assertEqual(json.loads((output/'summary.json').read_text())['unattempted'], 1)

    def test_missing_key_and_existing_output_do_not_call_or_create(self):
        output = self.root/'no-key'
        with patch.dict(os.environ, {}, clear=True), patch.object(gemini, '_request') as request:
            code, out, err = self.cli('--execute', '--output', str(output))
        self.assertEqual(code, 2); self.assertIn('GEMINI_API_KEY', err)
        self.assertFalse(output.exists()); request.assert_not_called()
        output.mkdir(); (output/'marker').write_text('keep')
        with patch.object(gemini, '_request') as request:
            self.assertEqual(self.cli('--execute', '--output', str(output))[0], 2)
        request.assert_not_called(); self.assertEqual((output/'marker').read_text(), 'keep')

    def test_smoke_and_redacted_untrusted_response(self):
        output = self.root/'smoke'
        text = json.dumps({'priority': 'low', 'rationale': 'fake-priority-key',
                           'needs_review': False, 'missing_information': []})
        with patch.object(gemini, '_request', return_value=interaction(text=text)) as request:
            code, out, err = self.cli('--execute', '--smoke', '--output', str(output))
        self.assertEqual(code, 0, err); self.assertEqual(request.call_count, 1)
        manifest = json.loads((output/'run-manifest.json').read_text())
        self.assertEqual((manifest['source_total'], manifest['selection']), (3, 'smoke'))
        record = json.loads((output/'results/R01.json').read_text())
        self.assertEqual(record['prediction']['rationale'], '[REDACTED]')

    def test_invalid_json_schema_and_missing_status_save_failure(self):
        samples = [(interaction(text='broken'), 'invalid_json'),
                   (interaction(text='{}'), 'invalid_schema'), ({'steps': []}, 'invalid_response')]
        for index, (data, status) in enumerate(samples):
            output = self.root/f'bad-{index}'
            with patch.object(gemini, '_request', return_value=data):
                code, out, err = self.cli('--execute', '--output', str(output))
            self.assertEqual(code, 2, err)
            self.assertEqual(json.loads((output/'results/R01.json').read_text())['status'], status)

    def test_bad_settings_fail_before_http(self):
        for index, options in enumerate([['--model', 'bad-model'], ['--timeout', 'nan'],
                                         ['--max-tokens', '8193'], ['--interval', '-1']]):
            output = self.root/f'settings-{index}'
            with patch.object(gemini, '_request') as request:
                self.assertEqual(self.cli('--execute', '--output', str(output), *options)[0], 2)
            request.assert_not_called(); self.assertFalse(output.exists())

    def test_request_start_interval_is_applied_without_retries(self):
        from triage_agent import priority_gemini
        client = priority_gemini.GeminiPriorityClient()
        client.set_api_key('fake-priority-key')
        with patch.object(gemini, '_request', return_value=interaction()) as request, \
                patch.object(priority_gemini.time, 'monotonic', side_effect=[100, 105, 113]), \
                patch.object(priority_gemini.time, 'sleep') as sleep:
            client(title='one', body='body')
            client(title='two', body='body')
        sleep.assert_called_once_with(8)
        self.assertEqual(request.call_count, 2)

    def test_json_escaped_key_is_redacted_after_decoding(self):
        output = self.root/'escaped-key'
        text = json.dumps({'priority': 'low', 'rationale': 'fake-priority-key',
                           'needs_review': False, 'missing_information': []}).replace('-', r'\u002d')
        with patch.object(gemini, '_request', return_value=interaction(text=text)):
            code, out, err = self.cli('--execute', '--output', str(output))
        self.assertEqual(code, 0, err)
        record = json.loads((output/'results/R01.json').read_text())
        self.assertEqual(record['prediction']['rationale'], '[REDACTED]')
        self.assertNotIn('fake-priority-key', ''.join(p.read_text() for p in output.rglob('*') if p.is_file()))


if __name__ == '__main__':
    main()
