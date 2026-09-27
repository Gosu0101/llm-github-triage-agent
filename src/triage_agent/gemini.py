"""#14: 분류 담당자의 프롬프트를 실행하는 Gemini 호출·응답 처리 계층."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
from http.client import HTTPException
import json
import math
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"
ISSUE_TYPES = ("bug", "feature-request", "question")
RETRYABLE = {"rate_limited", "server_error", "timeout", "network_error"}
USAGE_FIELDS = ("total_input_tokens", "total_output_tokens", "total_thought_tokens", "total_tokens")


@dataclass(frozen=True)
class GeminiSettings:
    model: str = "gemini-3.8-flash"
    timeout_seconds: float = 45
    max_output_tokens: int = 1024
    max_input_chars: int = 12000
    max_retries: int = 0
    truncate_body: bool = False
    allow_review: bool = False

    def validate(self) -> None:
        if not re.fullmatch(r"gemini-[a-z0-9.-]+", self.model):
            raise ValueError("Invalid Gemini model identifier")
        if not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be between 0 and 60")
        for name, low, high in (("max_retries", 0, 2), ("max_output_tokens", 1, 8192),
                                ("max_input_chars", 1, 100000)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} is outside the supported range")


def response_schema(allow_review: bool = False) -> dict[str, Any]:
    """보류 표현은 선택적으로 켜는 제안이며 분류 기준은 호출자가 제공한다."""
    properties: dict[str, Any] = {
        "type": {"type": "string", "enum": list(ISSUE_TYPES)},
        "rationale": {"type": "string"},
    }
    if allow_review:
        properties["type"] = {"type": ["string", "null"], "enum": [*ISSUE_TYPES, None]}
        properties["needs_review"] = {"type": "boolean"}
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def validate_prediction(value: Any, allow_review: bool = False) -> bool:
    expected = {"type", "rationale"} | ({"needs_review"} if allow_review else set())
    if not isinstance(value, dict) or set(value) != expected:
        return False
    if not isinstance(value["rationale"], str) or not value["rationale"].strip():
        return False
    if allow_review:
        if type(value["needs_review"]) is not bool:
            return False
        if value["needs_review"]:
            return value["type"] is None
    return isinstance(value["type"], str) and value["type"] in ISSUE_TYPES


def _json_loads(text: str) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise ValueError("Nonstandard JSON constant")

    return json.loads(text, object_pairs_hook=object_pairs, parse_constant=invalid_constant)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # API 키가 다른 호스트에 전달되지 않도록 redirect를 거부한다.
        return None


class _CallError(Exception):
    def __init__(self, status: str, http_status: int | None = None,
                 retry_after: float | None = None):
        super().__init__(status)
        self.status = status
        self.http_status = http_status
        self.retry_after = retry_after


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0, delay) if math.isfinite(delay) else None


def _request(payload: dict[str, Any], api_key: str, timeout: float) -> dict[str, Any]:
    request = Request(ENDPOINT, data=json.dumps(payload).encode("utf-8"), method="POST",
                      headers={"Content-Type": "application/json", "x-goog-api-key": api_key})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(2_000_001)
    except HTTPError as error:
        code = error.code
        delay = _retry_after(error.headers.get("Retry-After")) if error.headers else None
        error.close()
        status = ("rate_limited" if code == 429 else "auth_error" if code in (401, 403)
                  else "server_error" if code >= 500 else "api_error")
        # API 오류 본문·요청 헤더·예외 전문은 결과나 로그로 넘기지 않는다.
        raise _CallError(status, code, delay) from None
    except TimeoutError:
        raise _CallError("timeout") from None
    except URLError as error:
        status = "timeout" if isinstance(error.reason, TimeoutError) else "network_error"
        raise _CallError(status) from None
    except (OSError, HTTPException):
        raise _CallError("network_error") from None
    if len(raw) > 2_000_000:
        raise _CallError("invalid_response", 200)
    try:
        data = _json_loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise _CallError("invalid_json", 200) from None
    if not isinstance(data, dict):
        raise _CallError("invalid_response", 200)
    return data


def _extract(data: dict[str, Any], allow_review: bool) -> tuple[str, dict | None]:
    if data.get("status") != "completed":
        return "incomplete_response", None
    steps = data.get("steps")
    if not isinstance(steps, list):
        return "invalid_response", None
    parts = []
    for step in steps:
        if not isinstance(step, dict):
            return "invalid_response", None
        if step.get("type") != "model_output":
            continue
        content = step.get("content")
        if not isinstance(content, list):
            return "invalid_response", None
        for block in content:
            if not isinstance(block, dict):
                return "invalid_response", None
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
    if not parts:
        return "invalid_response", None
    try:
        prediction = _json_loads("".join(parts))
    except (ValueError, RecursionError):
        return "invalid_json", None
    if not validate_prediction(prediction, allow_review):
        return "invalid_schema", None
    return "success", prediction


class GeminiClient:
    def __init__(self, api_key: str, settings: GeminiSettings | None = None):
        if not api_key or not api_key.strip() or any(c.isspace() for c in api_key):
            raise ValueError("GEMINI_API_KEY must contain a nonempty key without whitespace")
        self._api_key = api_key
        self.settings = settings or GeminiSettings()
        self.settings.validate()

    def _redact(self, value: Any) -> Any:
        if isinstance(value, str):
            return value.replace(self._api_key, "[REDACTED]")
        if isinstance(value, dict):
            return {key: self._redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        return value

    def generate(self, *, title: str, body: str | None, instruction: str,
                 prompt_version: str) -> dict[str, Any]:
        """제목·본문만 입력으로 받고, 실패 시 prediction=None을 반환한다.

        repository/number/정답 필드는 호출자가 외부에서 보관한다. instruction은
        분류 담당자가 제공한다. 보류 표현과 길이/재시도 기본값은 팀 협의 대상이다.
        """
        if not isinstance(title, str) or not isinstance(body, (str, type(None))):
            raise ValueError("title/body must be strings; body may be null")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("instruction must not be empty")
        if not isinstance(prompt_version, str) or not prompt_version.strip():
            raise ValueError("prompt_version must not be empty")
        body = body or ""
        original_body_chars = len(body)
        room = self.settings.max_input_chars - len(title) - len(instruction)
        rejected = room < 0 or (len(body) > room and not self.settings.truncate_body)
        if not rejected and len(body) > room:
            body = body[:room]
        model_input = json.dumps({"title": title, "body": body}, ensure_ascii=False)
        record: dict[str, Any] = {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "provider": "google", "model_requested": self.settings.model,
            "prompt_version": prompt_version,
            "prompt_sha256": hashlib.sha256(instruction.encode()).hexdigest(),
            "input_sha256": hashlib.sha256(model_input.encode()).hexdigest(),
            "settings": asdict(self.settings),
            "input": {"title_chars": len(title), "body_original_chars": original_body_chars,
                      "body_sent_chars": 0 if rejected else len(body),
                      "truncated": not rejected and len(body) < original_body_chars,
                      "request_attempted": False},
            "status": "input_too_long" if rejected else "pending",
            "prediction": None, "attempts": [], "latency_seconds": 0.0,
        }
        if rejected:
            return self._redact(record)
        payload = {
            "model": self.settings.model, "input": model_input,
            "system_instruction": instruction, "store": False,
            "response_format": {"type": "text", "mime_type": "application/json",
                                "schema": response_schema(self.settings.allow_review)},
            "generation_config": {"max_output_tokens": self.settings.max_output_tokens,
                                  "thinking_level": "low"},
        }
        started = time.monotonic()
        for index in range(self.settings.max_retries + 1):
            attempt_start = time.monotonic()
            attempt: dict[str, Any] = {"number": index + 1}
            delay = None
            try:
                data = _request(payload, self._api_key, self.settings.timeout_seconds)
                status, prediction = _extract(data, self.settings.allow_review)
                usage = data.get("usage")
                attempt.update(http_status=200, usage={
                    k: usage[k] for k in USAGE_FIELDS
                    if isinstance(usage, dict) and type(usage.get(k)) is int and usage[k] >= 0
                })
                record["prediction"] = prediction
                returned_model = data.get("model")
                if isinstance(returned_model, str) and re.fullmatch(r"gemini-[a-z0-9.-]+", returned_model):
                    record["model_returned"] = returned_model
            except _CallError as error:
                status = error.status
                delay = error.retry_after
                attempt.update(http_status=error.http_status, retry_after_seconds=delay)
            attempt.update(status=status, latency_seconds=round(time.monotonic()-attempt_start, 3))
            record["attempts"].append(attempt)
            record["status"] = status
            record["input"]["request_attempted"] = True
            if status not in RETRYABLE or index == self.settings.max_retries:
                break
            delay = max(2 ** (index + 1), delay or 0)
            # 긴 Retry-After는 임의로 줄이지 않고 호출자에게 결과를 반환한다.
            if delay > 30:
                break
            time.sleep(delay)
        record["latency_seconds"] = round(time.monotonic()-started, 3)
        # 외부 응답에 키가 포함되더라도 로그·CLI에 노출하지 않는다.
        return self._redact(record)
