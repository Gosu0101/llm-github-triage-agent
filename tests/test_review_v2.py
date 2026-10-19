import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_classification_review import make_bundle


class ReviewV2Tests(unittest.TestCase):
    def setUp(self):
        from triage_agent import review_v2
        self.module = review_v2
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        make_bundle(self.root)
        from triage_agent import review_bundle as base
        package = base.load_bundle(self.root)
        records = []
        for row in package['dev'] + package['reserve']:
            records.append({'item_id':row['item_id'], 'status':'success',
                'prediction':{'type':'question','rationale':'Fixture','needs_review':False},
                'input_sha256':base.input_hash(row['input']),
                'prompt_sha256':base.digest(package['prompt'].encode())})
        files = {'classification-v2-review.txt':'Frozen v2',
            'reference/v1-gemini-results.jsonl':''.join(json.dumps(r)+'\n' for r in records),
            'reference/v1-local-results.jsonl':''.join(json.dumps(r)+'\n' for r in records),
            'local-settings.json':'{"confirmation":"historical-user-report"}'}
        hashes = {}
        for name,text in files.items():
            (self.root/name).write_text(text)
            hashes[name] = base.digest((self.root/name).read_bytes())
        (self.root/'v2-preflight.json').write_text(json.dumps({'hashes':hashes,'prompt_version':'classification-v2-review'}))

    def test_offline_validation_needs_no_credentials_or_transport(self):
        with contextlib.redirect_stdout(io.StringIO()), patch.object(self.module,'run',side_effect=AssertionError('No execution')):
            self.assertEqual(self.module.main(['--bundle',str(self.root)]),0)

    def test_v2_tampering_is_rejected_before_execution(self):
        (self.root/'classification-v2-review.txt').write_text('Changed')
        with contextlib.redirect_stderr(io.StringIO()), patch.object(self.module,'run',side_effect=AssertionError('No execution')):
            self.assertEqual(self.module.main(['--bundle',str(self.root),'--execute']),2)

    def call_local_cli(self, output, *options, fail_at=None):
        from triage_agent import review_local
        requests = []

        def response(url, payload, timeout):
            self.assertEqual(url, 'http://127.0.0.1:1234/v1/chat/completions')
            self.assertEqual(payload['messages'][0]['content'], 'Frozen v2')
            self.assertEqual(set(json.loads(payload['messages'][1]['content'])), {'title', 'body'})
            requests.append((payload, timeout))
            prediction = {'type': 'question' if len(requests) <= 18 else 'bug',
                          'rationale': 'Synthetic transport response', 'needs_review': False}
            return {'model': 'supergemma4-26b-uncensored-v2',
                    'choices': [{'finish_reason': 'length' if len(requests) == fail_at else 'stop',
                                 'message': {'content': json.dumps(prediction)}}],
                    'usage': {'prompt_tokens': 20, 'completion_tokens': 30, 'total_tokens': 50}}

        with patch.object(self.module, 'BOUNDARIES', ('microsoft/vscode#100', 'microsoft/vscode#101')), \
                patch.object(review_local, 'request_json', side_effect=response), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            code = self.module.main(['--bundle', str(self.root), '--provider', 'local',
                                     '--execute', '--output', str(output), *options])
        return code, requests, err.getvalue()

    def test_local_repeats_are_planned_offline_without_transport(self):
        from triage_agent import review_local
        with patch.object(review_local, 'request_json', side_effect=AssertionError('No network')), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = self.module.main(['--bundle', str(self.root), '--provider', 'local',
                                     '--boundary-repeats', '3', '--max-tokens', '4096', '--timeout', '600'])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())['planned_calls'], 24)
        self.assertEqual(json.loads(out.getvalue())['network_calls'], 0)

    def test_local_cli_separates_eighteen_primary_and_six_extra_results(self):
        output = self.root / 'local-repeats'
        code, requests, err = self.call_local_cli(output, '--boundary-repeats', '3',
                                                 '--max-tokens', '4096', '--timeout', '600')
        self.assertEqual(code, 0, err)
        summary = json.loads((output / 'summary.json').read_text())
        self.assertEqual((summary['primary_calls'], summary['repeat_calls'], summary['transport_attempts']),
                         (18, 6, 24))
        self.assertEqual(summary['comparison']['dev']['total'], 15)
        self.assertEqual(summary['comparison']['dev']['correct'], 5)
        self.assertEqual(len(list((output / 'results').glob('*.json'))), 18)
        self.assertEqual(sorted(p.name for p in (output / 'stability').glob('*.json')),
                         ['100-1.json', '100-2.json', '100-3.json', '101-1.json', '101-2.json', '101-3.json'])
        primary = json.loads((output / 'results/100.json').read_text())
        repeated = json.loads((output / 'stability/100-1.json').read_text())
        self.assertEqual(primary['prediction']['type'], 'question')
        self.assertEqual(repeated['prediction']['type'], 'bug')
        self.assertEqual(repeated['execution_group'], 'stability')
        self.assertEqual(repeated['usage']['total_tokens'], 50)
        manifest = json.loads((output / 'run-manifest.json').read_text())
        self.assertEqual(manifest['settings']['max_tokens'], 4096)
        self.assertEqual(manifest['settings']['automatic_retries'], 0)
        self.assertEqual(manifest['timeout_seconds'], 600)
        self.assertEqual(manifest['boundary_repeats_per_item'], 3)
        self.assertEqual(len(requests), 24)
        self.assertTrue(all(payload['max_tokens'] == 4096 and timeout == 600 for payload, timeout in requests))

    def test_local_repeat_failure_is_saved_and_stops_without_retry(self):
        output = self.root / 'local-repeat-failure'
        code, requests, err = self.call_local_cli(output, '--boundary-repeats', '3', fail_at=19)
        self.assertEqual(code, 2, err)
        self.assertTrue((output / 'summary.json').exists(), err)
        summary = json.loads((output / 'summary.json').read_text())
        self.assertEqual((summary['primary_calls'], summary['repeat_calls'], summary['transport_attempts']),
                         (18, 1, 19))
        self.assertEqual(summary['stopped'], {'group': 'stability', 'item_id': 'microsoft/vscode#100',
                                             'status': 'incomplete_response'})
        self.assertEqual(summary['comparison']['dev']['correct'], 5)
        failed = json.loads((output / 'stability/100-1.json').read_text())
        self.assertEqual(failed['finish_reason'], 'length')
        self.assertIsNone(failed['prediction'])
        self.assertEqual(len(failed['attempts']), 1)
        self.assertEqual(len(list((output / 'results').glob('*.json'))), 18)
        self.assertEqual(len(list((output / 'stability').glob('*.json'))), 1)
        self.assertEqual(len(requests), 19)

    def test_local_smoke_ignores_repeats_and_keeps_one_primary_result(self):
        output = self.root / 'local-smoke'
        code, requests, err = self.call_local_cli(output, '--boundary-repeats', '3', '--smoke')
        self.assertEqual(code, 0, err)
        summary = json.loads((output / 'summary.json').read_text())
        self.assertEqual((summary['primary_calls'], summary['repeat_calls']), (1, 0))
        self.assertEqual(len(requests), 1)
        self.assertEqual(list((output / 'stability').glob('*.json')), [])
        manifest = json.loads((output / 'run-manifest.json').read_text())
        self.assertEqual(manifest['boundary_repeats_per_item'], 0)

    def test_local_default_executes_eighteen_with_original_generation_limits(self):
        output = self.root / 'local-default'
        code, requests, err = self.call_local_cli(output)
        self.assertEqual(code, 0, err)
        summary = json.loads((output / 'summary.json').read_text())
        self.assertEqual((summary['primary_calls'], summary['repeat_calls']), (18, 0))
        self.assertEqual(len(requests), 18)
        self.assertTrue(all(payload['max_tokens'] == 2048 and timeout == 300 for payload, timeout in requests))

    def test_local_out_of_range_repeats_are_rejected_before_transport(self):
        for repeats in ('-1', '4'):
            with self.subTest(repeats=repeats):
                output = self.root / ('invalid-' + repeats)
                code, requests, _ = self.call_local_cli(output, '--boundary-repeats', repeats)
                self.assertEqual(code, 2)
                self.assertEqual(requests, [])
                self.assertFalse(output.exists())

    def test_regular_reserve_and_repeats_are_separate_and_failure_is_preserved(self):
        package=self.module.load(self.root,'gemini')
        package['boundary_ids']=[package['dev'][0]['item_id'],package['dev'][1]['item_id']]
        class Client:
            settings={}
            def generate(self,**kwargs):
                if kwargs['prompt_version'] != 'classification-v2-review':
                    raise AssertionError('Wrong prompt version')
                return {'status':'success','prediction':{'type':'question','rationale':'Fixture','needs_review':False},
                    'attempts':[{'number':1,'status':'success','http_status':200}],
                    'input_sha256':'fixture','prompt_sha256':'fixture'}
        with contextlib.redirect_stdout(io.StringIO()):
            report=self.module.run(package,self.root/'run',Client(),3)
        self.assertEqual(report['primary_calls'],18)
        self.assertEqual(report['repeat_calls'],6)
        self.assertEqual(report['comparison']['dev']['total'],15)
        self.assertNotIn('reference_question_matches',report['comparison']['reserve'])
        self.assertEqual(len(list((self.root/'run'/'results').glob('*.json'))),18)
        self.assertEqual(len(list((self.root/'run'/'stability').glob('*.json'))),6)
        self.assertEqual((self.root/'run'/'classification-v1-review.txt').read_text(),'Frozen v1')
        self.assertEqual((self.root/'run'/'classification-v2-review.txt').read_text(),'Frozen v2')
        class Failed(Client):
            def generate(self,**kwargs):
                return {'status':'network_error','prediction':None,'attempts':[{'number':1,'status':'network_error','http_status':None}]}
        with contextlib.redirect_stdout(io.StringIO()):
            report=self.module.run(package,self.root/'failed',Failed(),3)
        self.assertTrue(report['stopped'])
        self.assertEqual(report['primary_calls'],1)
        self.assertEqual(report['repeat_calls'],0)
        self.assertEqual(report['comparison']['dev']['unattempted'],14)
        self.assertTrue((self.root/'failed'/'summary.json').exists())
