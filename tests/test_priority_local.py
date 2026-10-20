"""Real priority CLI/client with the HTTP boundary replaced by recorded responses."""
import contextlib
import importlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_priority_review import digest, response


class PriorityLocalTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('triage_agent.priority_local'),
                             'Live priority adapter is not implemented yet')
        self.local = importlib.import_module('triage_agent.priority_local')
        self.review = importlib.import_module('triage_agent.priority_review')
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inputs = self.root/'inputs.json'; self.classifications = self.root/'classified.jsonl'
        self.rows = []
        for i in range(1, 4):
            value = {'title': f'Report {i}', 'body': 'Unmodified report body.'}
            self.rows.append({'case_id': f'R{i:02}', 'item_id': f'microsoft/vscode#{i}',
                              'origin': 'frozen_dev_input', 'input': value,
                              'input_sha256': digest(value)})
        self.inputs.write_text(json.dumps({'schema_version':'priority-preparation-inputs-v0',
                                          'cases':self.rows}), encoding='utf-8')
        records = [{'item_id':r['item_id'], 'input_sha256':r['input_sha256'], 'status':'success',
                    'prediction':{'type':'question' if i == 1 else 'bug',
                                  'needs_review':False,'rationale':'Saved classification'}}
                   for i,r in enumerate(self.rows)]
        self.classifications.write_text(''.join(json.dumps(r)+'\n' for r in records), encoding='utf-8')

    def cli(self, *options):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = self.review.main(['--inputs',str(self.inputs),
                                     '--classifications',str(self.classifications),*options])
        return code, out.getvalue(), err.getvalue()

    def test_offline_plan_loads_prompt_and_settings_without_http(self):
        with patch.object(self.local, 'request_json', side_effect=AssertionError('No HTTP')):
            code, out, err = self.cli('--max-tokens','4096','--timeout','600')
        self.assertEqual(code, 0, err)
        plan = json.loads(out)
        self.assertEqual((plan['mode'],plan['eligible_count'],plan['network_calls']),('validated',2,0))
        self.assertEqual(plan['prompt_version'], 'priority-bug-v0.1-draft')
        self.assertEqual(plan['prompt_sha256'], 'dd074db28862a2b19b4eee62561a0752a0e2da18bb68d690a101e37348491f69')

    def test_execute_sends_exact_input_and_schema_then_preserves_settings(self):
        output=self.root/'live-shaped'; calls=[]
        def transport(url,payload,timeout):
            calls.append((url,payload,timeout))
            return response(None if len(calls)==1 else 'medium')
        with patch.object(self.local,'request_json',side_effect=transport):
            code,out,err=self.cli('--execute','--output',str(output),'--max-tokens','4096','--timeout','600')
        self.assertEqual(code,0,err)
        self.assertEqual(len(calls),2)
        for i, (url,payload,timeout) in enumerate(calls):
            self.assertEqual(url,'http://127.0.0.1:1234/v1/chat/completions')
            self.assertEqual(timeout,600)
            self.assertEqual(json.loads(payload['messages'][1]['content']),self.rows[0 if i==0 else 2]['input'])
            self.assertEqual(payload['messages'][0]['role'],'system')
            self.assertEqual(payload['temperature'],0)
            self.assertEqual(payload['max_tokens'],4096)
            self.assertIs(payload['stream'],False)
            schema=payload['response_format']['json_schema']
            self.assertTrue(schema['strict'])
            self.assertEqual(set(schema['schema']['required']),
                             {'priority','rationale','needs_review','missing_information'})
        summary=json.loads((output/'summary.json').read_text())
        self.assertEqual((summary['mode'],summary['success'],summary['review'],summary['network_calls'],
                          summary['simulated_calls']),('local',2,1,2,0))
        self.assertEqual(summary['excluded']['not_applicable'],1)
        self.assertNotIn('accuracy',summary)
        manifest=json.loads((output/'run-manifest.json').read_text())
        self.assertEqual(manifest['provider'],'lm-studio-local')
        self.assertEqual(manifest['settings']['automatic_retries'],0)
        self.assertEqual(manifest['settings']['max_tokens'],4096)
        self.assertEqual(manifest['timeout_seconds'],600)
        self.assertEqual(manifest['context_length_reported'],8192)
        saved=(output/'priority-bug-v0.1-draft.txt').read_text(encoding='utf-8')
        self.assertEqual(saved,calls[0][1]['messages'][0]['content'])
        first=json.loads((output/'results/R01.json').read_text())
        self.assertEqual(first['provider'],'lm-studio-local')
        self.assertEqual(first['prompt_sha256'],manifest['prompt_sha256'])
        self.assertEqual(first['input_sha256'],self.rows[0]['input_sha256'])
        self.assertEqual(first['usage']['total_tokens'],50)
        self.assertFalse(first['input']['truncated'])

    def test_live_length_failure_records_usage_and_stops_without_retry(self):
        output=self.root/'failure'; calls=[]
        def transport(url,payload,timeout):
            calls.append(payload);return response(finish='length')
        with patch.object(self.local,'request_json',side_effect=transport):
            code,out,err=self.cli('--execute','--output',str(output))
        self.assertEqual(code,2,err);self.assertEqual(len(calls),1)
        summary=json.loads((output/'summary.json').read_text())
        self.assertEqual((summary['failed'],summary['review'],summary['unattempted'],summary['network_calls']),
                         (1,0,1,1))
        record=json.loads((output/'results/R01.json').read_text())
        self.assertEqual(record['status'],'incomplete_response')
        self.assertEqual(record['usage']['completion_tokens'],30)
        self.assertIsNone(record['prediction'])

    def test_transport_error_saves_failure_summary_and_drops_exception_text(self):
        output=self.root/'transport-error'
        from triage_agent.review_local import LocalError
        with patch.object(self.local,'request_json',side_effect=LocalError('network_error')) as transport:
            code,out,err=self.cli('--execute','--output',str(output))
        self.assertEqual(code,2,err);self.assertEqual(transport.call_count,1)
        self.assertEqual(json.loads((output/'summary.json').read_text())['failed'],1)
        self.assertEqual(json.loads((output/'results/R03.json').read_text())['status'],'unattempted')

    def test_smoke_selects_one_eligible_case_and_records_full_source_count(self):
        output=self.root/'smoke'
        with patch.object(self.local,'request_json',return_value=response('low')) as transport:
            code,out,err=self.cli('--execute','--smoke','--output',str(output))
        self.assertEqual(code,0,err);self.assertEqual(transport.call_count,1)
        summary=json.loads((output/'summary.json').read_text())
        self.assertEqual((summary['total'],summary['eligible_count'],summary['network_calls']),(1,1,1))
        self.assertEqual(sorted(p.name for p in (output/'results').glob('*.json')),['R01.json'])
        manifest=json.loads((output/'run-manifest.json').read_text())
        self.assertEqual((manifest['source_total'],manifest['selection']),(3,'smoke'))

    def test_bad_settings_missing_output_and_empty_plan_fail_before_http_or_output(self):
        for name, options in [('remote',['--base-url','http://example.com/v1']),
                              ('timeout',['--timeout','nan']),('negative',['--max-tokens','-1']),
                              ('no-input-room',['--max-tokens','8192','--context-length','8192']),
                              ('budget',['--max-tokens','8193','--context-length','8192'])]:
            output=self.root/name
            with self.subTest(name=name),patch.object(self.local,'request_json',side_effect=AssertionError('No HTTP')):
                code,out,err=self.cli('--execute','--output',str(output),*options)
            self.assertEqual(code,2);self.assertFalse(output.exists())
        with patch.object(self.local,'request_json',side_effect=AssertionError('No HTTP')):
            self.assertEqual(self.cli('--execute')[0],2)
        self.classifications.write_text('',encoding='utf-8')
        output=self.root/'no-eligible'
        with patch.object(self.local,'request_json',side_effect=AssertionError('No HTTP')):
            self.assertEqual(self.cli('--execute','--output',str(output))[0],2)
        self.assertFalse(output.exists())

    def test_prompt_plus_input_length_is_checked_before_output(self):
        self.rows[0]['input']['body']='x'*11500
        self.rows[0]['input_sha256']=digest(self.rows[0]['input'])
        self.inputs.write_text(json.dumps({'schema_version':'priority-preparation-inputs-v0','cases':self.rows}),encoding='utf-8')
        records=[json.loads(x) for x in self.classifications.read_text().splitlines()]
        records[0]['input_sha256']=self.rows[0]['input_sha256']
        self.classifications.write_text(''.join(json.dumps(r)+'\n' for r in records),encoding='utf-8')
        output=self.root/'too-long'
        with patch.object(self.local,'request_json',side_effect=AssertionError('No HTTP')):
            self.assertEqual(self.cli('--execute','--output',str(output))[0],2)
        self.assertFalse(output.exists())

    def test_existing_output_is_never_contacted_or_overwritten(self):
        output=self.root/'existing';output.mkdir();(output/'marker').write_text('keep')
        with patch.object(self.local,'request_json',side_effect=AssertionError('No HTTP')):
            self.assertEqual(self.cli('--execute','--output',str(output))[0],2)
        self.assertEqual((output/'marker').read_text(),'keep')


if __name__=='__main__':unittest.main()
