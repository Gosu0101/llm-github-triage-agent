import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


def make_bundle(root):
    hashes = {}
    def put(name, text):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    dev = [{'item_id': f'microsoft/vscode#{100+i}', 'input': {'title': f'Issue {i}', 'body': 'Body'}} for i in range(15)]
    reserve = [{'item_id': f'microsoft/vscode#{200+i}', 'input': {'title': 'Reserve', 'body': 'Body'}} for i in range(3)]
    gold = [{'item_id': row['item_id'], 'mapped_label': ('bug', 'feature-request', 'question')[i//5]} for i,row in enumerate(dev)]
    old_prompt = 'Frozen v0'
    baseline = [{'item_id': row['item_id'], 'status': 'success', 'prediction': {'type': gold[i]['mapped_label']},
        'input_sha256': hashlib.sha256(json.dumps(row['input'], ensure_ascii=False).encode()).hexdigest(),
        'prompt_sha256': hashlib.sha256(old_prompt.encode()).hexdigest()} for i,row in enumerate(dev)]
    for name,rows in [('inputs/dev-inputs.jsonl',dev),('inputs/reserve-inputs.jsonl',reserve),('reference/dev-gold.jsonl',gold),('reference/v0-results.jsonl',baseline)]:
        put(name, ''.join(json.dumps(row)+'\n' for row in rows))
    put('reference/run-manifest.v0.json', '{}')
    put('reference/classification-v0-review.txt', old_prompt)
    put('classification-v1-review.txt', 'Frozen v1')
    manifest = {'schema_version':1, 'hashes':hashes, 'dev_count':15, 'reserve_ids':[r['item_id'] for r in reserve],
        'dataset_version':'test-dev', 'prompt_version':'classification-v1-review', 'retry_note':'test',
        'settings':{'model':'gemini-3.8-flash','timeout_seconds':45,'max_output_tokens':1024,'max_input_chars':12000,
                    'max_retries':1,'truncate_body':False,'allow_review':True}}
    (root/'preflight.json').write_text(json.dumps(manifest), encoding='utf-8')


class ClassificationReviewTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('triage_agent.classification_review'),
                             'Package review entry point is missing')
        from triage_agent import classification_review
        self.runner = classification_review
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root/'bundle'
        self.bundle.mkdir()
        make_bundle(self.bundle)

    def call(self, *options):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = self.runner.main(['--bundle',str(self.bundle),*options])
        return code,out.getvalue(),err.getvalue()

    def test_offline_providers_do_not_read_credentials_or_call_transport(self):
        from triage_agent import gemini, review_local
        with patch.object(gemini,'_request',side_effect=AssertionError('network')), patch.object(review_local,'request_json',side_effect=AssertionError('network')), patch.object(self.runner,'load_env_file',side_effect=AssertionError('env')):
            for provider in ['gemini','local']:
                code,out,err=self.call('--provider',provider,'--env-file',str(self.root/'missing.env'))
                self.assertEqual(code,0,err)
                self.assertEqual(json.loads(out)['network_calls'],0)
        self.assertFalse((self.bundle/'runs').exists())

    def test_changed_bundle_and_gold_leakage_fail_before_network(self):
        path=self.bundle/'inputs/dev-inputs.jsonl'
        original=path.read_text()
        path.write_text(original+'\n')
        self.assertEqual(self.call()[0],2)
        rows=[json.loads(line) for line in original.splitlines()]
        rows[0]['input']['mapped_label']='bug'
        path.write_text('\n'.join(json.dumps(row) for row in rows)+'\n')
        manifest=json.loads((self.bundle/'preflight.json').read_text())
        manifest['hashes']['inputs/dev-inputs.jsonl']=hashlib.sha256(path.read_bytes()).hexdigest()
        (self.bundle/'preflight.json').write_text(json.dumps(manifest))
        code,out,err=self.call()
        self.assertEqual(code,2)
        self.assertIn('title/body',err)

    def test_gemini_requires_key_only_when_execution_is_requested(self):
        with patch.dict(os.environ,{},clear=True):
            code,out,err=self.call('--execute')
        self.assertEqual(code,2)
        self.assertIn('GEMINI_API_KEY',err)

    def test_local_paired_run_retains_model_baseline_and_refuses_overwrite(self):
        from triage_agent import review_local
        def reply(url,payload,timeout):
            data=json.loads(payload['messages'][1]['content'])
            self.assertEqual(set(data),{'title','body'})
            self.assertIn(payload['messages'][0]['content'],['Frozen v0','Frozen v1'])
            return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'type':'question','rationale':'Transport fixture','needs_review':False})}}]}
        output=self.root/'run'
        with patch.object(review_local,'request_json',side_effect=reply):
            code,out,err=self.call('--provider','local','--execute','--output',str(output))
        self.assertEqual(code,0,err)
        report=json.loads((output/'summary.json').read_text())
        self.assertEqual(report['calls'],33)
        self.assertTrue(report['paired_predictions_complete'])
        self.assertEqual(report['phases']['v1']['dev']['retained_v0_correct'],5)
        original=(output/'summary.json').read_bytes()
        self.assertEqual(self.call('--provider','local','--execute','--output',str(output))[0],2)
        self.assertEqual((output/'summary.json').read_bytes(),original)

    def test_local_length_failure_is_saved_and_stops(self):
        from triage_agent import review_local
        response={'choices':[{'finish_reason':'length','message':{'content':'{}'}}]}
        output=self.root/'run'
        with patch.object(review_local,'request_json',return_value=response):
            code,out,err=self.call('--provider','local','--execute','--output',str(output))
        self.assertEqual(code,2)
        summary=json.loads((output/'summary.json').read_text())
        self.assertEqual(summary['calls'],1)
        self.assertEqual(summary['stopped']['status'],'incomplete_response')
        self.assertEqual(summary['phases']['v1']['dev']['unattempted'],15)

    def test_gemini_pacing_keeps_retry_attempts_and_restores_transport(self):
        from triage_agent import gemini
        output=self.root/'run'
        calls=[]
        def reply(payload,key,timeout):
            calls.append(json.loads(payload['input']))
            if len(calls)==1:
                raise gemini._CallError('server_error',503)
            return {'status':'completed','steps':[{'type':'model_output','content':[{'type':'text','text':json.dumps({'type':'question','rationale':'Transport fixture','needs_review':False})}]}]}
        with patch.dict(os.environ,{'GEMINI_API_KEY':'test-key-only'}), patch.object(gemini,'_request',side_effect=reply) as transport, patch.object(self.runner.time,'sleep') as sleep:
            code,out,err=self.call('--execute','--output',str(output))
            self.assertIs(gemini._request,transport)
        self.assertEqual(code,0,err)
        self.assertEqual(len(calls),19)
        self.assertTrue(all(set(row)=={'title','body'} for row in calls))
        report=json.loads((output/'summary.json').read_text())
        self.assertEqual(report['calls'],19)
        self.assertEqual(report['dev']['total'],15)
        self.assertEqual(report['reserve']['total'],3)
        notes=json.loads((output/'execution-notes.json').read_text())
        self.assertEqual(notes['minimum_attempt_start_interval_seconds'],13)
        self.assertEqual(notes['actual_api_attempts'],19)
        self.assertTrue(any(call.args[0]>0 for call in sleep.call_args_list))
        self.assertNotIn('test-key-only',''.join(p.read_text(encoding='utf-8-sig') for p in output.rglob('*') if p.is_file()))

    def test_project_default_bundle_needs_no_path_flag_for_either_provider(self):
        directory=self.root/'data'/'classification-review'
        directory.mkdir(parents=True)
        make_bundle(directory)
        for provider in ['gemini','local']:
            result=subprocess.run([sys.executable,'-B','-m','triage_agent.classification_review','--provider',provider],
                                  cwd=self.root,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['network_calls'],0)
            self.assertEqual(json.loads(result.stdout)['dev_items'],15)
        self.assertFalse((self.root/'data'/'llm').exists())

    def test_missing_bundle_names_required_file_without_network(self):
        result=subprocess.run([sys.executable,'-B','-m','triage_agent.classification_review',
                               '--bundle',str(self.root/'absent')],capture_output=True,text=True)
        self.assertEqual(result.returncode,2)
        self.assertIn('preflight.json',result.stderr)
        self.assertNotIn('Traceback',result.stderr)

    def test_module_command_works_without_machine_specific_path(self):
        result=subprocess.run([sys.executable,'-B','-m','triage_agent.classification_review','--bundle',str(self.bundle)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['dev_items'],15)

    def test_remote_local_endpoint_is_rejected_and_review_stays_in_denominator(self):
        self.assertEqual(self.call('--provider','local','--base-url','https://example.com/v1')[0],2)
        from triage_agent import review_bundle
        gold=[{'item_id':'a','mapped_label':'bug'},{'item_id':'b','mapped_label':'question'}]
        records=[{'item_id':'a','status':'success','prediction':{'type':None}},{'item_id':'b','status':'timeout','prediction':None}]
        report,_=review_bundle.compare(records,gold,[],[])
        self.assertEqual(report['dev']['total'],2)
        self.assertEqual(report['dev']['review'],1)
        self.assertEqual(report['dev']['failed'],1)

if __name__=='__main__':
    unittest.main()
