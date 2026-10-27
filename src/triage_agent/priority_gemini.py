"""Gemini Interactions adapter for priority; construction never reads credentials."""
from importlib.resources import files
import json
import math
import re
import time

from . import gemini, priority_contract as contract, review_bundle as base, review_local
from .priority_local import PROMPT_SHA256, PROMPT_VERSION


class GeminiPriorityClient:
    def __init__(self, *, model='gemini-3.8-flash', max_tokens=4096, timeout=60, interval=13):
        gemini.GeminiSettings(model=model, max_output_tokens=max_tokens, timeout_seconds=timeout).validate()
        if not math.isfinite(interval) or not 0 <= interval <= 60:
            raise ValueError('Gemini request interval must be 0..60 seconds')
        self.model, self.timeout, self.interval = model, timeout, interval
        self._api_key = None
        self._previous_start = None
        self.prompt = files('triage_agent').joinpath('prompts', PROMPT_VERSION+'.txt').read_text(encoding='utf-8')
        if base.digest(self.prompt.encode('utf-8')) != PROMPT_SHA256:
            raise ValueError('Packaged priority prompt hash mismatch')
        self.settings = {'temperature': 0, 'max_output_tokens': max_tokens, 'thinking_level': 'low',
                         'structured_output': 'json_schema', 'automatic_retries': 0, 'store': False,
                         'minimum_attempt_start_interval_seconds': interval}

    def set_api_key(self, key):
        if not isinstance(key, str) or not key.strip() or any(c.isspace() for c in key):
            raise ValueError('GEMINI_API_KEY must be configured; no API calls made')
        self._api_key = key

    def validate_plan(self, plan):
        for row in plan['rows']:
            if row['state'] == 'eligible' and len(self.prompt) + sum(len(v) for v in row['input'].values()) > 12000:
                raise ValueError(f"{row['case_id']}: prompt plus input too long; truncation is forbidden")

    def metadata(self):
        return {'provider': 'google', 'model_requested': self.model, 'settings': self.settings,
                'timeout_seconds': self.timeout, 'prompt_version': PROMPT_VERSION,
                'prompt_sha256': PROMPT_SHA256,
                'latency_note': 'Includes any request pacing wait; not pure inference time.',
                'usage_note': 'Gemini input/output/thought/total counters; total may include thought tokens.'}

    def __call__(self, *, title, body):
        if self._api_key is None:
            raise ValueError('GEMINI_API_KEY must be configured; no API calls made')
        payload = {'model': self.model, 'input': json.dumps({'title': title, 'body': body}, ensure_ascii=False),
                   'system_instruction': self.prompt, 'store': False,
                   'response_format': {'type': 'text', 'mime_type': 'application/json', 'schema': contract.SCHEMA},
                   'generation_config': {'temperature': 0, 'max_output_tokens': self.settings['max_output_tokens'],
                                         'thinking_level': 'low'}}
        if self._previous_start is not None:
            time.sleep(max(0.0, self.interval - (time.monotonic() - self._previous_start)))
        self._previous_start = time.monotonic()
        try:
            data = gemini._request(payload, self._api_key, self.timeout)
        except gemini._CallError as error:
            raise review_local.LocalError(error.status, error.http_status) from None
        return self._normalize(data)

    def _normalize(self, data):
        """Adapt envelopes only; shared priority validation decides JSON/schema validity."""
        if not isinstance(data, dict):
            return {}
        response = {'choices': []}
        usage = data.get('usage')
        if isinstance(usage, dict):
            mapping = {'total_input_tokens': 'prompt_tokens', 'total_output_tokens': 'completion_tokens',
                       'total_thought_tokens': 'total_thought_tokens', 'total_tokens': 'total_tokens'}
            response['usage'] = {target: usage[source] for source, target in mapping.items()
                                 if type(usage.get(source)) is int and usage[source] >= 0}
        model = data.get('model')
        if isinstance(model, str) and re.fullmatch(r'gemini-[a-z0-9.-]+', model):
            response['model'] = model.replace(self._api_key, '[REDACTED]')
        status = data.get('status')
        if status in ('incomplete', 'in_progress', 'failed', 'cancelled', 'requires_action'):
            response['choices'] = [{'finish_reason': status}]
            return response
        if status != 'completed' or not isinstance(data.get('steps'), list):
            return response
        parts = []
        for step in data['steps']:
            if not isinstance(step, dict):
                return response
            if step.get('type') != 'model_output':
                continue
            if not isinstance(step.get('content'), list):
                return response
            for block in step['content']:
                if not isinstance(block, dict):
                    return response
                if block.get('type') == 'text' and isinstance(block.get('text'), str):
                    parts.append(block['text'])
        if parts:
            text = ''.join(parts)
            try:
                decoded = contract.strict_json(text)
            except (ValueError, RecursionError):
                # Shared validation records invalid JSON without storing its raw text.
                pass
            else:
                # Decode first: JSON escapes must not bypass credential redaction.
                text = json.dumps(self._redact(decoded), ensure_ascii=False)
            response['choices'] = [{'finish_reason': 'stop', 'message': {'content': text}}]
        return response

    def _redact(self, value):
        if isinstance(value, str):
            return value.replace(self._api_key, '[REDACTED]')
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, dict):
            return {key: self._redact(item) for key, item in value.items()}
        return value
