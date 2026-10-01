"""실제 API 비용 없이 호출 경계·실패 처리·정보 분리를 확인한다."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
from http.client import IncompleteRead
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from triage_agent.gemini import GeminiClient, GeminiSettings, _CallError, _request
from triage_agent.gemini_example import main


KEY = "fake-test-key-never-real"


def response(prediction=None, status="completed"):
    if prediction is None:
        prediction = {"type": "bug", "rationale": "Saving crashes the app."}
    return {"status": status, "model": "gemini-3.8-flash",
            "steps": [{"type": "thought", "signature": "do-not-log"},
                      {"type": "model_output", "content": [
                          {"type": "text", "text": json.dumps(prediction)}]}],
            "usage": {"total_input_tokens": 20, "total_output_tokens": 10,
                      "total_tokens": 30, "unexpected_secret": KEY}}


class GeminiTests(unittest.TestCase):
    def call(self, settings=None, **changes):
        args = dict(title="App crashes", body="Saving closes the app", instruction="External prompt", prompt_version="v0")
        args.update(changes)
        return GeminiClient(KEY, settings).generate(**args)

    @patch("triage_agent.gemini._request")
    def test_success_preserves_usage_and_only_sends_title_body(self, request):
        request.return_value = response()
        result = self.call()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["prediction"]["type"], "bug")
        payload = request.call_args.args[0]
        self.assertEqual(set(json.loads(payload["input"])), {"title", "body"})
        self.assertEqual(payload["system_instruction"], "External prompt")
        self.assertFalse(payload["store"])
        self.assertEqual(result["attempts"][0]["usage"]["total_tokens"], 30)
        self.assertNotIn(KEY, json.dumps(result))
        self.assertNotIn("do-not-log", json.dumps(result))

    @patch("triage_agent.gemini._request")
    def test_bad_outputs_never_become_predictions(self, request):
        invalid = [[], {}, {"type": "other", "rationale": "reason"},
                   {"type": "bug", "rationale": ""}, {"type": "bug", "rationale": 3},
                   {"type": None, "rationale": "unknown"},
                   {"type": "bug", "rationale": "ok", "extra": True}]
        for value in invalid:
            with self.subTest(value=value):
                request.return_value = response(value)
                result = self.call()
                self.assertEqual(result["status"], "invalid_schema")
                self.assertIsNone(result["prediction"])

    @patch("triage_agent.gemini._request")
    def test_malformed_and_duplicate_key_json_are_rejected(self, request):
        for text in ('not json', '{"type":"bug","type":"question","rationale":"x"}',
                     '{"type":"bug","rationale":NaN}'):
            data = response()
            data["steps"][1]["content"][0]["text"] = text
            request.return_value = data
            result = self.call()
            self.assertEqual(result["status"], "invalid_json")
            self.assertIsNone(result["prediction"])

    @patch("triage_agent.gemini._request")
    def test_incomplete_or_missing_model_text_cannot_succeed(self, request):
        for data in (response(status="incomplete"), {"status": "completed", "steps": None},
                     {"status": "completed", "steps": [{"type": "model_output", "content": []}]}):
            request.return_value = data
            result = self.call()
            self.assertNotEqual(result["status"], "success")
            self.assertIsNone(result["prediction"])

    @patch("triage_agent.gemini._request")
    def test_review_contract_requires_explicit_opt_in_and_consistency(self, request):
        request.return_value = response({"type": None, "rationale": "Missing details", "needs_review": True})
        self.assertEqual(self.call()["status"], "invalid_schema")
        settings = GeminiSettings(allow_review=True)
        self.assertEqual(self.call(settings)["status"], "success")
        for value in ({"type": "bug", "rationale": "x", "needs_review": True},
                      {"type": None, "rationale": "x", "needs_review": False},
                      {"type": "bug", "rationale": "x", "needs_review": 0}):
            request.return_value = response(value)
            self.assertEqual(self.call(settings)["status"], "invalid_schema")

    @patch("triage_agent.gemini._request")
    def test_length_limit_rejects_before_network_or_explicitly_truncates(self, request):
        request.return_value = response()
        settings = GeminiSettings(max_input_chars=40)
        result = self.call(settings, body="a" * 80)
        self.assertEqual(result["status"], "input_too_long")
        request.assert_not_called()
        result = self.call(replace(settings, truncate_body=True), body="a" * 80)
        self.assertTrue(result["input"]["truncated"])
        self.assertLess(result["input"]["body_sent_chars"], 80)
        payload = request.call_args.args[0]
        issue = json.loads(payload["input"])
        self.assertEqual(len(issue["title"])+len(issue["body"])+len(payload["system_instruction"]), 40)

    @patch("triage_agent.gemini._request")
    def test_errors_are_separate_and_default_has_no_retries(self, request):
        for status in ("timeout", "network_error", "server_error", "rate_limited", "auth_error"):
            request.reset_mock()
            request.side_effect = _CallError(status)
            result = self.call()
            self.assertEqual(result["status"], status)
            self.assertIsNone(result["prediction"])
            request.assert_called_once()

    @patch("triage_agent.gemini._request")
    def test_key_is_redacted_even_from_provider_prediction(self, request):
        request.return_value = response({"type": "bug", "rationale": KEY})
        result = self.call()
        self.assertNotIn(KEY, json.dumps(result))
        self.assertEqual(result["prediction"]["rationale"], "[REDACTED]")

    @patch("triage_agent.gemini.time.sleep")
    @patch("triage_agent.gemini._request")
    def test_retry_is_bounded_and_keeps_failure_history(self, request, sleep):
        request.side_effect = [_CallError("server_error", 503, 3), response()]
        result = self.call(GeminiSettings(max_retries=1))
        self.assertEqual(result["status"], "success")
        self.assertEqual([a["status"] for a in result["attempts"]], ["server_error", "success"])
        sleep.assert_called_once_with(3)
        request.side_effect = _CallError("server_error", 503)
        request.reset_mock()
        result = self.call(GeminiSettings(max_retries=2))
        self.assertEqual(len(result["attempts"]), 3)
        self.assertIsNone(result["prediction"])

    @patch("triage_agent.gemini.time.sleep")
    @patch("triage_agent.gemini._request")
    def test_long_retry_after_or_auth_error_does_not_retry(self, request, sleep):
        for error in (_CallError("rate_limited", 429, 120), _CallError("auth_error", 403)):
            request.reset_mock()
            request.side_effect = error
            self.call(GeminiSettings(max_retries=2))
            request.assert_called_once()
        sleep.assert_not_called()

    @patch("triage_agent.gemini.build_opener")
    def test_http_failure_drops_body_and_preserves_status(self, opener):
        for code, expected in ((429, "rate_limited"), (503, "server_error"), (403, "auth_error"), (400, "api_error")):
            opener.return_value.open.side_effect = HTTPError("https://example.invalid", code, KEY, {"Retry-After": "7"}, io.BytesIO(KEY.encode()))
            with self.assertRaises(_CallError) as caught:
                _request({}, KEY, 1)
            self.assertEqual(caught.exception.status, expected)
            self.assertEqual(caught.exception.retry_after, 7)
            self.assertNotIn(KEY, str(caught.exception))

    @patch("triage_agent.gemini.build_opener")
    def test_transport_and_outer_json_failures(self, opener):
        for error, expected in ((TimeoutError(KEY), "timeout"), (URLError(TimeoutError()), "timeout"),
                                (URLError(KEY), "network_error"),
                                (IncompleteRead(b'partial'), "network_error")):
            opener.return_value.open.side_effect = error
            with self.assertRaises(_CallError) as caught:
                _request({}, KEY, 1)
            self.assertEqual(caught.exception.status, expected)
        opener.return_value.open.side_effect = None
        opener.return_value.open.return_value.__enter__.return_value.read.return_value = b"<html>failure</html>"
        with self.assertRaises(_CallError) as caught:
            _request({}, KEY, 1)
        self.assertEqual(caught.exception.status, "invalid_json")

    def test_invalid_settings_fail_before_network(self):
        for changes in ({"max_retries": 3}, {"timeout_seconds": float('nan')},
                        {"max_output_tokens": 0}, {"max_input_chars": -1}):
            with self.assertRaises(ValueError):
                GeminiClient(KEY, GeminiSettings(**changes))

    @patch("triage_agent.gemini._request")
    def test_cli_loads_env_and_saves_result_without_key(self, request):
        request.return_value = response()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env = Path(directory)/'.env'
            env.write_text('GEMINI_API_KEY='+KEY)
            out = Path(directory)/'results'
            with redirect_stdout(io.StringIO()) as stdout:
                code = main(['--demo', '--env-file', str(env), '--output-dir', str(out)])
            self.assertEqual(code, 0)
            saved = next(out.glob('*.json'))
            self.assertNotIn(KEY, saved.read_text()+stdout.getvalue())
            self.assertEqual(json.loads(saved.read_text())["sample_kind"], "fictional_connection_demo")
            if os.name != 'nt':
                self.assertEqual(saved.stat().st_mode & 0o777, 0o600)

    @patch("triage_agent.gemini._request")
    def test_cli_rejects_answer_labels_before_network(self, request):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)
            (p/'issue.json').write_text(json.dumps({'title':'x', 'body':'y', 'labels':['bug']}))
            (p/'prompt.txt').write_text('prompt')
            with redirect_stderr(io.StringIO()):
                code=main(['--input', str(p/'issue.json'), '--prompt', str(p/'prompt.txt'), '--prompt-version', 'v0'])
            self.assertEqual(code, 2)
            request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
