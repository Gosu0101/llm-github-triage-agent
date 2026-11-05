"""Exercise the real CLI and adapters, replacing only the HTTP boundary."""
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from triage_agent import gemini, triage_pipeline


def interaction(prediction):
    return {'status': 'completed', 'model': 'gemini-3.8-flash',
            'steps': [{'type': 'model_output', 'content': [
                {'type': 'text', 'text': json.dumps(prediction)}]}],
            'usage': {'total_input_tokens': 10, 'total_output_tokens': 20,
                      'total_thought_tokens': 0, 'total_tokens': 30}}


def classification(kind):
    return interaction({'type': kind, 'rationale': 'Observed behavior only.',
                        'needs_review': kind is None})


def priority(grade=None):
    return interaction({'priority': grade, 'rationale': 'Confirmed impact.',
                        'needs_review': grade is None,
                        'missing_information': ['Can the task be completed?'] if grade is None else []})


class PipelineTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.inputs = self.root/'inputs.json'
        self.rows = []
        for i in range(4):
            value = {'title': f'VS Code issue {i}', 'body': 'Full issue body.'}
            digest = hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()
            self.rows.append({'case_id': f'R{i+1:02}', 'item_id': f'microsoft/vscode#{100+i}',
                              'origin': 'synthetic-test', 'input': value, 'input_sha256': digest})
        self.inputs.write_text(json.dumps({'schema_version': 'priority-preparation-inputs-v0',
                                          'cases': self.rows}), encoding='utf-8')

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = triage_pipeline.main(['--inputs', str(self.inputs), '--interval', '0', *args])
        return code, out.getvalue(), err.getvalue()

    def execute(self, responses, *args):
        output = self.root/'run'
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'fake-pipeline-key'}), \
                patch.object(gemini, '_request', side_effect=responses) as http:
            result = self.cli('--execute', '--output', str(output), *args)
        return output, result, http

    def test_offline_needs_no_env_and_reports_conditional_calls(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(gemini, '_request') as http:
            code, out, err = self.cli('--env-file', str(self.root/'missing.env'))
        self.assertEqual(code, 0, err)
        info = json.loads(out)
        self.assertEqual((info['classification_calls'], info['priority_calls_max'],
                          info['network_calls'], info['credentials_required']), (4, 4, 0, False))
        http.assert_not_called()
        self.assertFalse((self.root/'run').exists())

    def test_prompt_length_rejection_identifies_all_blockers_before_credentials_or_output(self):
        for index in (0, 2):
            self.rows[index]['input']['body'] = 'private-source-marker' + 'x'*10000
            self.rows[index]['input_sha256'] = hashlib.sha256(
                json.dumps(self.rows[index]['input'], ensure_ascii=False).encode()).hexdigest()
        self.inputs.write_text(json.dumps({'schema_version': 'priority-preparation-inputs-v0',
                                          'cases': self.rows}), encoding='utf-8')
        original = self.inputs.read_bytes(); output = self.root/'rejected-run'
        with patch.dict(os.environ, {}, clear=True), patch.object(gemini, '_request') as http, \
                patch('triage_agent.config.load_env_file') as env:
            code, out, err = self.cli('--execute', '--env-file', str(self.root/'missing.env'),
                                       '--output', str(output))
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        info = json.loads(err)
        self.assertEqual(info['reason'], 'prompt_input_too_long')
        self.assertEqual((info['network_calls'], info['credentials_required']), (0, False))
        self.assertEqual([r['case_id'] for r in info['blocked_items']], ['R01', 'R03'])
        for row in info['blocked_items']:
            self.assertEqual(row['total_chars'], row['input_chars']+row['prompt_chars'])
            self.assertEqual(row['excess_chars'], row['total_chars']-info['max_input_chars'])
            self.assertGreater(row['excess_chars'], 0)
        self.assertNotIn('private-source-marker', err)
        self.assertFalse(output.exists()); http.assert_not_called(); env.assert_not_called()
        self.assertEqual(self.inputs.read_bytes(), original)

    def test_exact_length_limit_passes_and_smoke_ignores_unselected_long_rows(self):
        plan, client = triage_pipeline.load(self.inputs)
        budget = plan['classification_settings'].max_input_chars
        prompt_size = max(len(plan['classification_prompt']), len(client.prompt))
        self.rows[0]['input']['body'] = 'x'*(budget-prompt_size-len(self.rows[0]['input']['title']))
        self.rows[2]['input']['body'] = 'x'*10000
        for row in self.rows:
            row['input_sha256'] = hashlib.sha256(json.dumps(row['input'], ensure_ascii=False).encode()).hexdigest()
        self.inputs.write_text(json.dumps({'schema_version': 'priority-preparation-inputs-v0',
                                          'cases': self.rows}), encoding='utf-8')
        with patch.dict(os.environ, {}, clear=True), patch.object(gemini, '_request') as http:
            code, out, err = self.cli('--smoke')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['classification_calls'], 1)
        http.assert_not_called()

    def test_chain_routes_bug_only_and_saves_both_stages_without_reason_leak(self):
        output, (code, out, err), http = self.execute([
            classification('question'), classification('bug'), priority(),
            classification(None), classification('feature-request')])
        self.assertEqual(code, 0, err)
        records = [json.loads((output/f'results/R{i:02}.json').read_text()) for i in range(1, 5)]
        self.assertEqual([r['priority']['status'] for r in records],
                         ['not_applicable', 'success', 'classification_review', 'not_applicable'])
        self.assertTrue(records[1]['needs_review']); self.assertTrue(records[2]['needs_review'])
        self.assertIsNone(records[1]['priority']['prediction']['priority'])
        self.assertEqual(json.loads((output/'classification/results/R02.json').read_text())['prediction']['type'], 'bug')
        summary = json.loads((output/'summary.json').read_text())
        self.assertEqual((summary['network_calls'], summary['classification_calls'],
                          summary['priority_calls'], summary['failed'], summary['review']), (5, 4, 1, 0, 2))
        self.assertEqual(http.call_count, 5)
        for index, row_index in enumerate([0, 1, 1, 2, 3]):
            payload = http.call_args_list[index].args[0]
            self.assertEqual(json.loads(payload['input']), self.rows[row_index]['input'])
            self.assertNotIn('Observed behavior only.', payload['input'])
        manifest = json.loads((output/'run-manifest.json').read_text())
        for stage in ('classification', 'priority'):
            prompt = (output/f'{stage}-prompt.txt').read_bytes()
            self.assertEqual(hashlib.sha256(prompt).hexdigest(), manifest[stage]['prompt_sha256'])
        self.assertNotIn('fake-pipeline-key', ''.join(p.read_text() for p in output.rglob('*.json')))

    def test_classification_failure_preserves_failure_and_unattempted_rows(self):
        output, (code, _, err), http = self.execute([gemini._CallError('auth_error', 401)])
        self.assertEqual(code, 2, err); self.assertEqual(http.call_count, 1)
        first = json.loads((output/'results/R01.json').read_text())
        self.assertEqual((first['classification']['status'], first['priority']['status']),
                         ('auth_error', 'upstream_failed'))
        self.assertEqual(json.loads((output/'results/R02.json').read_text())['status'], 'unattempted')
        summary = json.loads((output/'summary.json').read_text())
        self.assertEqual((summary['failed'], summary['unattempted'], summary['stopped']['stage']),
                         (1, 3, 'classification'))

    def test_priority_failure_preserves_successful_classification_and_stops(self):
        output, (code, _, err), http = self.execute([
            classification('bug'), gemini._CallError('rate_limited', 429)])
        self.assertEqual(code, 2, err); self.assertEqual(http.call_count, 2)
        first = json.loads((output/'results/R01.json').read_text())
        self.assertEqual((first['classification']['status'], first['priority']['status']),
                         ('success', 'rate_limited'))
        self.assertIsNone(first['priority']['prediction'])
        summary = json.loads((output/'summary.json').read_text())
        self.assertEqual((summary['failed'], summary['unattempted'], summary['stopped']['stage']),
                         (1, 3, 'priority'))

    def test_smoke_selects_one_input_but_can_make_two_calls(self):
        output, (code, _, err), http = self.execute([
            classification('bug'), priority('medium')], '--smoke')
        self.assertEqual(code, 0, err); self.assertEqual(http.call_count, 2)
        self.assertEqual(len(list((output/'results').glob('*.json'))), 1)
        self.assertEqual(json.loads((output/'summary.json').read_text())['total'], 1)

    def test_preflight_rejects_existing_output_hash_mismatch_and_prompt_overflow(self):
        output = self.root/'existing'; output.mkdir()
        for mutation in ('existing', 'hash', 'length'):
            with self.subTest(mutation=mutation):
                rows = json.loads(self.inputs.read_text())
                if mutation == 'hash': rows['cases'][0]['input_sha256'] = 'wrong'
                if mutation == 'length':
                    value = rows['cases'][0]['input']; value['body'] = 'x'*11500
                    rows['cases'][0]['input_sha256'] = hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()
                self.inputs.write_text(json.dumps(rows), encoding='utf-8')
                target = output if mutation == 'existing' else self.root/mutation
                with patch.dict(os.environ, {'GEMINI_API_KEY': 'fake-pipeline-key'}), patch.object(gemini, '_request') as http:
                    code, _, _ = self.cli('--execute', '--output', str(target))
                self.assertEqual(code, 2); http.assert_not_called()
                if mutation != 'existing': self.assertFalse(target.exists())
                self.inputs.write_text(json.dumps({'schema_version': 'priority-preparation-inputs-v0', 'cases': self.rows}), encoding='utf-8')

    def test_global_pacing_covers_classification_priority_and_retry(self):
        with patch('time.monotonic', return_value=0), patch('time.sleep') as sleep:
            output, (code, _, err), http = self.execute([
                gemini._CallError('server_error', 500), classification('bug'), priority('medium')],
                '--smoke', '--interval', '13')
        self.assertEqual(code, 0, err); self.assertEqual(http.call_count, 3)
        self.assertGreaterEqual(sum(call.args[0] == 13 for call in sleep.call_args_list), 2)
        self.assertEqual(json.loads((output/'summary.json').read_text())['network_calls'], 3)
