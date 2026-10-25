"""LM Studio adapter for the selected priority draft; never calls on construction."""
from __future__ import annotations

from importlib.resources import files
import json
import math
import re

from . import priority_contract as contract, review_bundle as base, review_local
from .review_local import request_json

PROMPT_VERSION = 'priority-bug-v0.3-draft'
PROMPT_SHA256 = 'dd62f2444751b9b08ac91fbe4d55e9f3dbae555131cb81a3f5b6235650cfa54b'


class LocalPriorityClient:
    def __init__(self, *, base_url='http://127.0.0.1:1234/v1',
                 model='supergemma4-26b-uncensored-v2', max_tokens=4096,
                 timeout=600, context_length=8192):
        self.base_url = review_local.check_base_url(base_url)
        if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}', model):
            raise ValueError('Invalid local model identifier')
        if (type(max_tokens) is not int or not 1 <= max_tokens <= 8192
                or type(context_length) is not int or context_length <= 0 or max_tokens >= context_length):
            raise ValueError('Invalid generation or reported context budget')
        if not math.isfinite(timeout) or not 0 < timeout <= 600:
            raise ValueError('Timeout must be finite and between 0 and 600 seconds')
        self.model = model
        self.timeout = timeout
        self.context_length = context_length
        self.prompt = files('triage_agent').joinpath('prompts', PROMPT_VERSION+'.txt').read_text(encoding='utf-8')
        if base.digest(self.prompt.encode('utf-8')) != PROMPT_SHA256:
            raise ValueError('Packaged priority prompt hash mismatch')
        self.settings = {'temperature': 0, 'max_tokens': max_tokens, 'stream': False,
                         'structured_output': 'json_schema_strict', 'automatic_retries': 0}

    def validate_plan(self, plan):
        for row in plan['rows']:
            if row['state'] == 'eligible' and len(self.prompt) + sum(len(v) for v in row['input'].values()) > 12000:
                raise ValueError(f"{row['case_id']}: prompt plus input too long; truncation is forbidden")

    def metadata(self):
        return {'provider': 'lm-studio-local', 'model_requested': self.model,
                'base_url': self.base_url, 'settings': self.settings,
                'timeout_seconds': self.timeout, 'context_length_reported': self.context_length,
                'prompt_version': PROMPT_VERSION, 'prompt_sha256': PROMPT_SHA256,
                'context_token_check': 'Manual: verify actual prompt tokens + max_tokens on the serving PC.'}

    def __call__(self, *, title, body):
        payload = {
            'model': self.model,
            'messages': [{'role': 'system', 'content': self.prompt},
                         {'role': 'user', 'content': json.dumps({'title': title, 'body': body}, ensure_ascii=False)}],
            'temperature': 0, 'max_tokens': self.settings['max_tokens'], 'stream': False,
            'response_format': {'type': 'json_schema', 'json_schema': {
                'name': 'bug_priority', 'strict': True, 'schema': contract.SCHEMA}},
        }
        return request_json(self.base_url+'/chat/completions', payload, self.timeout)
