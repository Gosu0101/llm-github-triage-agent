"""Temporary priority contract: real validation/storage, simulated provider only."""
import contextlib
import copy
import hashlib
from http.client import IncompleteRead
import importlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def prediction(priority='medium'):
    return {'priority': priority, 'rationale': '작업은 가능하지만 반복 조작이 필요하다.',
            'needs_review': priority is None,
            'missing_information': ['작업을 완료할 수 있나요?'] if priority is None else []}


def response(priority='medium', finish='stop'):
    return {'model': 'simulated-only',
            'choices': [{'finish_reason': finish,
                         'message': {'content': json.dumps(prediction(priority))}}],
            'usage': {'prompt_tokens': 20, 'completion_tokens': 30, 'total_tokens': 50}}


class PriorityContractTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('triage_agent.priority_contract'),
                             'priority contract is not implemented yet')
        self.module = importlib.import_module('triage_agent.priority_contract')

    def test_grades_and_valid_holds(self):
        for grade in ('high', 'medium', 'low', None):
            with self.subTest(grade=grade):
                self.assertTrue(self.module.valid_prediction(prediction(grade)))
        value = prediction(None)
        value['missing_information'] = ['무엇이 실패하나요?', '손실이 있나요?', '우회가 있나요?']
        self.assertTrue(self.module.valid_prediction(value))

    def test_invalid_types_and_cross_field_combinations(self):
        invalid = [None, [], 'null']
        for field, value in [('priority', 'urgent'), ('priority', 'null'), ('priority', []),
                             ('needs_review', 1), ('needs_review', 'false'),
                             ('rationale', '  '), ('rationale', None),
                             ('missing_information', 'question'),
                             ('missing_information', ['question'])]:
            item = prediction(); item[field] = value; invalid.append(item)
        invalid.extend([dict(prediction(), extra='leak'),
                        {k: v for k, v in prediction().items() if k != 'rationale'},
                        dict(prediction(None), needs_review=False),
                        dict(prediction(), needs_review=True)])
        for questions in ([], ['  '], ['a', 'b', 'c', 'd'], [None], [1]):
            invalid.append(dict(prediction(None), missing_information=questions))
        for item in invalid:
            with self.subTest(item=item):
                self.assertFalse(self.module.valid_prediction(item))

    def test_strict_json_rejects_duplicate_nonstandard_and_wrapped_text(self):
        for text in ('{"priority":null,"priority":"low"}', '{"value":NaN}',
                     '{"value":Infinity}', '```json\n{}\n```', '{} explanation'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.module.strict_json(text)


class PriorityReviewTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('triage_agent.priority_review'),
                             'priority runner is not implemented yet')
        self.module = importlib.import_module('triage_agent.priority_review')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inputs = self.root/'inputs.json'
        self.classifications = self.root/'classification.jsonl'
        self.rows = []
        for i in range(1, 7):
            value = {'title': f'Case {i}', 'body': 'Original report, not an answer.'}
            self.rows.append({'case_id': f'G{i:02}', 'item_id': f'synthetic/G{i:02}',
                              'origin': 'synthetic_existing_conditions_generated_title',
                              'input': value, 'input_sha256': digest(value)})
        self.write_inputs()
        self.upstream = [self.classified(row, 'bug') for row in self.rows]
        self.upstream[1] = self.classified(self.rows[1], 'question')
        self.upstream[2] = self.classified(self.rows[2], None)
        self.upstream[3] = self.classified(self.rows[3], None, status='network_error')
        self.upstream.pop()  # Last input has no upstream record.
        self.write_upstream()

    def write_inputs(self):
        self.inputs.write_text(json.dumps({'schema_version': 'priority-preparation-inputs-v0',
                                          'cases': self.rows}), encoding='utf-8')

    def classified(self, row, kind, status='success'):
        return {'item_id': row['item_id'], 'input_sha256': row['input_sha256'],
                'status': status, 'prediction': {'type': kind, 'needs_review': kind is None,
                    'rationale': 'Synthetic classification fixture'} if status == 'success' else None}

    def write_upstream(self):
        self.classifications.write_text(''.join(json.dumps(x)+'\n' for x in self.upstream), encoding='utf-8')

    def load(self):
        return self.module.load(self.inputs, self.classifications)

    def test_planning_routes_all_states_and_sends_nothing(self):
        plan = self.load()
        self.assertEqual([r['state'] for r in plan['rows']],
                         ['eligible', 'not_applicable', 'classification_review',
                          'upstream_failed', 'eligible', 'not_ready'])
        self.assertEqual(plan['eligible_count'], 2)
        self.upstream[1] = self.classified(self.rows[1], 'feature-request')
        self.write_upstream()
        self.assertEqual(self.load()['rows'][1]['state'], 'not_applicable')

    def test_offline_cli_does_not_need_responses_output_or_credentials(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = self.module.main(['--inputs', str(self.inputs),
                                     '--classifications', str(self.classifications)])
        self.assertEqual(code, 0)
        summary = json.loads(out.getvalue())
        self.assertEqual((summary['mode'], summary['eligible_count'], summary['network_calls']),
                         ('validated', 2, 0))
        self.assertFalse((self.root/'results').exists())

    def test_invalid_inputs_hashes_and_classifications_fail_preflight(self):
        original_rows = copy.deepcopy(self.rows)
        original_upstream = copy.deepcopy(self.upstream)
        for mutation in ('input_label', 'extra_wrapper_key', 'hash', 'duplicate_id',
                         'duplicate_case', 'unsafe_case', 'body_type', 'long_body',
                         'classification_hash', 'duplicate_upstream', 'inconsistent_prediction',
                         'failed_with_prediction', 'unknown_status'):
            self.rows = copy.deepcopy(original_rows); self.upstream = copy.deepcopy(original_upstream)
            if mutation == 'input_label': self.rows[0]['input']['mapped_label'] = 'high'
            elif mutation == 'extra_wrapper_key': self.rows[0]['gold'] = 'high'
            elif mutation == 'hash': self.rows[0]['input_sha256'] = '0'*64
            elif mutation == 'duplicate_id': self.rows[1]['item_id'] = self.rows[0]['item_id']
            elif mutation == 'duplicate_case': self.rows[1]['case_id'] = self.rows[0]['case_id']
            elif mutation == 'unsafe_case': self.rows[0]['case_id'] = '../escape'
            elif mutation == 'body_type': self.rows[0]['input']['body'] = None
            elif mutation == 'long_body':
                self.rows[0]['input']['body'] = 'x'*12001
                self.rows[0]['input_sha256'] = digest(self.rows[0]['input'])
            elif mutation == 'classification_hash': self.upstream[0]['input_sha256'] = '0'*64
            elif mutation == 'duplicate_upstream': self.upstream.append(self.upstream[0])
            elif mutation == 'inconsistent_prediction': self.upstream[0]['prediction']['needs_review'] = True
            elif mutation == 'failed_with_prediction': self.upstream[0]['status'] = 'timeout'
            elif mutation == 'unknown_status': self.upstream[0]['status'] = 'made_up'
            self.write_inputs(); self.write_upstream()
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.load()

    def test_existing_classification_results_directory_is_supported(self):
        directory = self.root/'classification-results'; directory.mkdir()
        for i, record in enumerate(self.upstream):
            (directory/f'{i}.json').write_text(json.dumps(record), encoding='utf-8')
        plan = self.module.load(self.inputs, directory)
        self.assertEqual(plan['eligible_count'], 2)
        self.assertEqual(len(plan['classification_sources']), 5)

    def test_missing_upstream_is_not_a_hold_or_assumed_bug(self):
        plan = self.module.load(self.inputs)
        self.assertEqual(plan['eligible_count'], 0)
        def must_not_request(**value):
            self.fail('Missing classifications must never be requested')
        output = self.root/'not-ready'
        self.assertEqual(self.module.run(plan, output, must_not_request), 0)
        summary = json.loads((output/'summary.json').read_text())
        self.assertEqual((summary['excluded']['not_ready'], summary['review'],
                          summary['simulated_calls']), (6, 0, 0))

    def test_wrong_mock_order_or_duplicate_case_rejected_before_output(self):
        path = self.root/'wrong-order.json'
        entries = [{'case_id': r['case_id'], 'input_sha256': r['input_sha256'],
                    'response': response()} for r in (self.rows[4], self.rows[0])]
        path.write_text(json.dumps(entries), encoding='utf-8')
        with self.assertRaises(ValueError): self.module.mock_request(path, self.load())

    def test_windows_filename_collisions_and_reserved_names_are_rejected(self):
        for case_id in ('g01', 'CON', 'aux', 'COM1', 'LPT9', 'NUL', 'PRN'):
            self.rows[1]['case_id'] = case_id
            self.write_inputs()
            with self.subTest(case_id=case_id), self.assertRaises(ValueError):
                self.load()

    def test_input_hash_uses_title_body_order_before_mock_execution(self):
        value = self.rows[0]['input']
        self.rows[0]['input'] = {'body': value['body'], 'title': value['title']}
        self.write_inputs()
        plan = self.load()  # Canonical title/body hash is unchanged by file key order.
        self.assertEqual(list(plan['rows'][0]['input']), ['title', 'body'])
        self.rows[0]['input_sha256'] = digest(self.rows[0]['input'])
        self.upstream[0]['input_sha256'] = self.rows[0]['input_sha256']
        self.write_inputs(); self.write_upstream()
        with self.assertRaises(ValueError): self.load()

    def test_truncation_keeps_available_usage_and_no_partial_prediction(self):
        output = self.root/'truncated-usage'
        self.assertEqual(self.module.run(self.load(), output,
                                        lambda **value: response(finish='length')), 2)
        record = json.loads((output/'results/G01.json').read_text())
        self.assertEqual(record['usage']['completion_tokens'], 30)
        self.assertIsNone(record['prediction'])

    def test_success_and_hold_continue_and_store_separate_results(self):
        requests = []
        def request(**value):
            requests.append(value)
            return response(None if len(requests) == 1 else 'low')
        output = self.root/'priority-run'
        code = self.module.run(self.load(), output, request)
        self.assertEqual(code, 0)
        self.assertEqual(requests, [self.rows[0]['input'], self.rows[4]['input']])
        summary = json.loads((output/'summary.json').read_text(encoding='utf-8'))
        self.assertEqual((summary['total'], summary['eligible_count'], summary['simulated_calls'],
                          summary['success'], summary['review'], summary['failed'], summary['unattempted']),
                         (6, 2, 2, 2, 1, 0, 0))
        self.assertEqual(summary['network_calls'], 0)
        self.assertNotIn('accuracy', summary); self.assertNotIn('f1', summary)
        self.assertEqual(summary['grades'], {'high': 0, 'medium': 0, 'low': 1})
        records = [json.loads(p.read_text(encoding='utf-8')) for p in sorted((output/'results').glob('*.json'))]
        self.assertEqual(len(records), 6)
        self.assertEqual(records[0]['status'], 'success')
        self.assertIsNone(records[0]['prediction']['priority'])
        self.assertEqual(records[1]['status'], 'not_applicable')
        self.assertIsNone(records[1]['prediction'])
        self.assertEqual(records[4]['usage']['total_tokens'], 50)
        manifest = json.loads((output/'run-manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['mode'], 'mock')
        self.assertEqual(manifest['contract_status'], 'provisional')
        self.assertEqual(manifest['automatic_retries'], 0)
        self.assertEqual(self.classifications.read_text(encoding='utf-8'),
                         ''.join(json.dumps(x)+'\n' for x in self.upstream))
        with self.assertRaises(FileExistsError): self.module.run(self.load(), output, request)
        self.assertEqual(len(requests), 2)

    def test_failures_are_saved_stop_once_and_do_not_turn_into_review(self):
        for kind in ('length', 'bad_json', 'bad_schema', 'incomplete_read', 'timeout',
                     'network', 'missing_choices'):
            output = self.root/kind
            requests = []
            def request(**value):
                requests.append(value)
                if kind == 'incomplete_read': raise IncompleteRead(b'partial')
                if kind == 'timeout': raise TimeoutError('not for logging')
                if kind == 'network': raise OSError('not for logging')
                if kind == 'missing_choices': return {'choices': []}
                result = response(finish='length' if kind == 'length' else 'stop')
                if kind == 'bad_json': result['choices'][0]['message']['content'] = 'no JSON'
                if kind == 'bad_schema': result['choices'][0]['message']['content'] = '{}'
                return result
            with self.subTest(kind=kind):
                self.assertEqual(self.module.run(self.load(), output, request), 2)
                self.assertEqual(len(requests), 1)
                summary = json.loads((output/'summary.json').read_text(encoding='utf-8'))
                self.assertEqual((summary['failed'], summary['review'], summary['unattempted']), (1, 0, 1))
                first = json.loads((output/'results/G01.json').read_text(encoding='utf-8'))
                last = json.loads((output/'results/G05.json').read_text(encoding='utf-8'))
                self.assertIsNone(first['prediction'])
                self.assertEqual(first['attempts'][0]['number'], 1)
                self.assertEqual(last['status'], 'unattempted')
                self.assertEqual(last['attempts'], [])
                self.assertNotIn('not for logging', (output/'results/G01.json').read_text())
                expected = {'length':'incomplete_response', 'bad_json':'invalid_json',
                            'bad_schema':'invalid_schema', 'incomplete_read':'network_error',
                            'timeout':'timeout', 'network':'network_error',
                            'missing_choices':'invalid_response'}[kind]
                self.assertEqual(first['status'], expected)

    def test_mock_cli_validates_order_hash_and_required_output_before_creating_results(self):
        mock = self.root/'responses.json'
        rows = [self.rows[0], self.rows[4]]
        entries = [{'case_id': r['case_id'], 'input_sha256':r['input_sha256'],
                    'response':response('low')} for r in rows]
        mock.write_text(json.dumps(entries), encoding='utf-8')
        output = self.root/'mock-cli'
        args = ['--inputs',str(self.inputs),'--classifications',str(self.classifications),
                '--mock-responses',str(mock)]
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.module.main(args), 2)
        entries[0]['input_sha256'] = '0'*64
        mock.write_text(json.dumps(entries), encoding='utf-8')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.module.main(args+['--output',str(output)]), 2)
        self.assertFalse(output.exists())
        entries[0]['input_sha256'] = rows[0]['input_sha256']
        mock.write_text(json.dumps(entries), encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.module.main(args+['--output',str(output)]), 0)
        manifest = json.loads((output/'run-manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['mock_responses_sha256'], hashlib.sha256(mock.read_bytes()).hexdigest())
        self.assertEqual(json.loads((output/'summary.json').read_text())['simulated_calls'], 2)


if __name__ == '__main__':
    unittest.main()
