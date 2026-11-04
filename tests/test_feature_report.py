"""Offline feature joins must preserve provenance and incomplete work."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from triage_agent import feature_report, gemini, priority_review


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


class FeatureReportTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.issues = [self.case('R01', 1), self.case('R02', 2)]
        value = {'number': 3, 'title': 'PR title', 'body': 'Description',
                 'files': [{'filename': 'a.py', 'patch': '+change', 'patch_truncated': False}]}
        self.prs = [{'case_id': 'P01', 'item_id': 'microsoft/vscode#3', 'origin': 'synthetic-test',
                     'input': value, 'input_sha256': digest(value)}]
        self.write('issues.json', {'schema_version': 'priority-preparation-inputs-v0', 'cases': self.issues})
        self.write('prs.json', {'schema_version': 'pr-summary-inputs-v0', 'cases': self.prs})

    def case(self, case_id, number):
        value = {'title': 'Issue title', 'body': 'Complete body'}
        return {'case_id': case_id, 'item_id': f'microsoft/vscode#{number}', 'origin': 'synthetic-test',
                'input': value, 'input_sha256': digest(value)}

    def write(self, name, value):
        p = self.root/name; p.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8'); return p

    def result(self, row, prediction, status='success'):
        return {'item_id': row['item_id'], 'input_sha256': row['input_sha256'],
                'status': status, 'prediction': prediction}

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), patch.object(gemini, '_request') as http:
            code = feature_report.main(['--issue-inputs', str(self.root/'issues.json'),
                                        '--pr-inputs', str(self.root/'prs.json'), *args])
        http.assert_not_called(); return code, out.getvalue(), err.getvalue()

    def sources(self):
        cls = [self.result(r, {'type': 'bug', 'rationale': 'Observed failure', 'needs_review': False})
               for r in self.issues]
        pri = [self.result(r, {'priority': 'medium', 'rationale': 'Confirmed impact',
                              'needs_review': False, 'missing_information': []}) for r in self.issues]
        dup = self.result(self.issues[0], {'target_issue_number': 1, 'candidate_issue_number': 2,
                                          'verdict': 'related', 'confidence': 0.8,
                                          'evidence': ['Similar component'], 'rationale': 'Different failure'})
        dup.update(candidate_item_id=self.issues[1]['item_id'], candidate_input_sha256=self.issues[1]['input_sha256'])
        summary = self.result(self.prs[0], {'summary': 'Changes a.py', 'key_changes': ['A change'],
                                          'affected_areas': [], 'tests': [], 'risks': [],
                                          'breaking_changes': [], 'insufficient_information': False})
        for name, records in [('cls.json', cls), ('pri.json', pri), ('dup.json', [dup]), ('summary.json', [summary])]:
            self.write(name, records)
        return ['--classifications', str(self.root/'cls.json'), '--priorities', str(self.root/'pri.json'),
                '--duplicates', str(self.root/'dup.json'), '--pr-summaries', str(self.root/'summary.json')]

    def test_absent_features_stay_not_ready_and_create_nothing_by_default(self):
        code, out, err = self.cli()
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['states'], {'not_ready': 3})
        self.assertEqual(json.loads(out)['network_calls'], 0)
        self.assertFalse((self.root/'report').exists())

    def test_issue_and_pr_results_join_by_hash_and_remain_distinct(self):
        options = self.sources(); output = self.root/'report'
        code, _, err = self.cli(*options, '--output', str(output))
        self.assertEqual(code, 0, err)
        report = json.loads((output/'report.json').read_text(encoding='utf-8'))
        first, second, pr = report['items']
        self.assertEqual([r['status'] for r in report['items']], ['ready', 'partial', 'ready'])
        self.assertEqual(first['features']['duplicate']['results'][0]['prediction']['verdict'], 'related')
        self.assertEqual(second['features']['duplicate']['status'], 'not_ready')
        self.assertEqual(pr['features']['classification']['status'], 'not_applicable')
        self.assertEqual(pr['features']['pr_summary']['prediction']['summary'], 'Changes a.py')
        self.assertNotIn('body', json.dumps(report))
        self.assertEqual(report['network_calls'], 0)

    def test_wrong_target_candidate_hash_or_unrelated_record_refuses_output(self):
        options = self.sources(); original = json.loads((self.root/'dup.json').read_text())
        for field, value in [('input_sha256', 'wrong'), ('candidate_input_sha256', 'wrong'),
                             ('item_id', 'microsoft/vscode#99'), ('candidate_item_id', 'microsoft/vscode#1')]:
            with self.subTest(field=field):
                record = deepcopy(original); record[0][field] = value
                self.write('dup.json', record)
                output = self.root/field
                code, _, _ = self.cli(*options, '--output', str(output))
                self.assertEqual(code, 2); self.assertFalse(output.exists())

    def test_mock_priority_execution_provenance_survives_ready_report(self):
        options = self.sources()
        classifications = self.root/'classification-results'; classifications.mkdir()
        for index, record in enumerate(json.loads((self.root/'cls.json').read_text())):
            self.write(f'classification-results/{index}.json', record)
        predictions = json.loads((self.root/'pri.json').read_text())
        fixtures = self.write('mock.json', [
            {'case_id': row['case_id'], 'input_sha256': row['input_sha256'],
             'response': {'choices': [{'finish_reason': 'stop', 'message': {
                 'content': json.dumps(result['prediction'])}}]}}
            for row, result in zip(self.issues, predictions)])
        priority_output = self.root/'mock-priority'
        with redirect_stdout(io.StringIO()), patch.object(gemini, '_request') as http:
            code = priority_review.main(['--inputs', str(self.root/'issues.json'),
                '--classifications', str(classifications), '--mock-responses', str(fixtures),
                '--output', str(priority_output)])
        http.assert_not_called()
        self.assertEqual(code, 0)
        options[options.index('--priorities') + 1] = str(priority_output/'results')
        output = self.root/'mock-report'
        code, _, err = self.cli(*options, '--output', str(output))
        self.assertEqual(code, 0, err)
        report = json.loads((output/'report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['items'][0]['status'], 'ready')
        for row, result in zip(self.issues, report['items']):
            original = json.loads((priority_output/'results'/f"{row['case_id']}.json").read_text())
            saved = result['features']['priority']
            self.assertEqual((saved['mode'], saved['provider']), ('mock', 'mock'))
            self.assertEqual(saved['prediction'], original['prediction'])
        self.assertEqual(report['network_calls'], 0)

    def test_truncated_pr_input_marks_review_without_rewriting_model_output(self):
        options = self.sources(); self.prs[0]['input']['files'][0]['patch_truncated'] = True
        self.prs[0]['input_sha256'] = digest(self.prs[0]['input'])
        self.write('prs.json', {'schema_version': 'pr-summary-inputs-v0', 'cases': self.prs})
        records = json.loads((self.root/'summary.json').read_text()); records[0]['input_sha256'] = self.prs[0]['input_sha256']
        self.write('summary.json', records); output = self.root/'report'
        code, _, err = self.cli(*options, '--output', str(output))
        self.assertEqual(code, 0, err)
        pr = json.loads((output/'report.json').read_text(encoding='utf-8'))['items'][2]
        self.assertEqual(pr['status'], 'needs_review')
        self.assertIn('source_patch_truncated', pr['warnings'])
        self.assertFalse(pr['features']['pr_summary']['prediction']['insufficient_information'])
        self.assertEqual(json.loads((self.root/'summary.json').read_text()), records)

    def test_failed_and_unattempted_stages_are_not_model_abstention(self):
        self.write('cls.json', [self.result(self.issues[0], None, 'network_error'),
                                self.result(self.issues[1], None, 'unattempted')])
        code, out, err = self.cli('--classifications', str(self.root/'cls.json'))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['states'], {'failed': 1, 'partial': 1, 'not_ready': 1})

    def test_ambiguous_or_malformed_predictions_and_overwrite_are_rejected(self):
        options = self.sources()
        for name, mutate in [
            ('dup.json', lambda r: r[0]['prediction'].update(confidence=True)),
            ('summary.json', lambda r: r[0]['prediction'].update(insufficient_information='false')),
            ('cls.json', lambda r: r.append(deepcopy(r[0]))),
            ('cls.json', lambda r: (r[0].update(status='network_error'), r[0].pop('prediction'))),
            ('dup.json', lambda r: r[0]['prediction'].update(confidence=10**1000)),
            ('pri.json', lambda r: r[0]['prediction'].update(needs_review=True))]:
            original = (self.root/name).read_bytes(); records = json.loads(original); mutate(records)
            self.write(name, records)
            code, _, _ = self.cli(*options)
            self.assertEqual(code, 2, name); (self.root/name).write_bytes(original)
        output = self.root/'existing'; output.mkdir()
        self.assertEqual(self.cli(*options, '--output', str(output))[0], 2)
