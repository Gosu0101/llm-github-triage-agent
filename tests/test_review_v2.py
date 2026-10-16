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
