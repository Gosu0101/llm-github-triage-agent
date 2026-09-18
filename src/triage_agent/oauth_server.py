"""외부 웹 프레임워크 없이 실행하는 GitHub OAuth 로컬 검증 서버."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import threading
import time
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit
import webbrowser

from .collector import collect_repository, save_collection
from .config import OAuthSettings, save_env_value
from .github import GitHubClient, GitHubOAuthClient, GitHubRequestError


def _pkce_challenge(verifier: str) -> str:
    """PKCE verifier를 SHA-256으로 해시해 URL-safe challenge로 변환한다."""

    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class PendingAuthorization:
    """승인 시작 시 저장할 PKCE verifier와 생성 시각."""

    code_verifier: str
    created_at: float


class PendingAuthorizationStore:
    """state와 PKCE verifier를 일회성으로 보관하는 메모리 저장소.

    로컬 검증 목적이라 별도 DB를 두지 않는다. 서버를 재시작하면 모든 항목이
    사라지며, 여러 요청 thread가 동시에 접근할 수 있어 Lock을 사용한다.
    """

    def __init__(self, *, ttl_seconds: int = 600, clock: Callable[[], float] = time.monotonic):
        """만료 시간과 테스트에서 교체 가능한 시계 함수를 설정한다."""

        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._items: dict[str, PendingAuthorization] = {}
        self._lock = threading.Lock()

    def create(self) -> tuple[str, str, str]:
        """새 state, verifier, challenge를 생성하고 verifier를 보관한다."""

        state = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(64)
        with self._lock:
            self._remove_expired()
            self._items[state] = PendingAuthorization(code_verifier, self._clock())
        return state, code_verifier, _pkce_challenge(code_verifier)

    def consume(self, state: str) -> str | None:
        """유효한 state를 한 번만 소비하고 연결된 verifier를 반환한다."""

        if not state:
            return None
        with self._lock:
            self._remove_expired()
            # pop을 사용하므로 같은 callback을 재전송해도 두 번째 요청은 실패한다.
            pending = self._items.pop(state, None)
        return pending.code_verifier if pending else None

    def _remove_expired(self) -> None:
        """TTL을 넘긴 승인 요청을 메모리에서 제거한다."""

        now = self._clock()
        expired = [
            state
            for state, pending in self._items.items()
            if now - pending.created_at > self.ttl_seconds
        ]
        for state in expired:
            self._items.pop(state, None)


def create_handler(settings: OAuthSettings) -> type[BaseHTTPRequestHandler]:
    """설정과 승인 상태를 공유하는 HTTP request handler class를 만든다."""

    oauth = GitHubOAuthClient(
        settings.client_id,
        settings.client_secret,
        settings.redirect_uri,
        settings.scope,
    )
    pending = PendingAuthorizationStore()
    # callback query를 주소창에서 제거한 뒤 보여줄 최종 결과를 잠시 보관한다.
    result_holder: dict[str, Any] = {}
    result_lock = threading.Lock()

    class OAuthHandler(BaseHTTPRequestHandler):
        """`/login`, callback, 상태 확인 endpoint를 처리한다."""

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            """요청 경로에 따라 JSON 응답, GitHub 이동, callback을 처리한다."""

            parsed = urlsplit(self.path)
            if parsed.path == "/":
                self._json_response(
                    200,
                    {
                        "service": "GitHub OAuth verification",
                        "login_url": "/login",
                        "callback_path": settings.callback_path,
                    },
                )
                return
            if parsed.path == "/health":
                self._json_response(200, {"status": "ok"})
                return
            if parsed.path == "/login":
                # state/verifier는 서버에만 두고 브라우저에는 state/challenge만 보낸다.
                state, _, challenge = pending.create()
                self.send_response(302)
                self.send_header("Location", oauth.authorization_url(state=state, code_challenge=challenge))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            if parsed.path == "/result":
                with result_lock:
                    status = int(result_holder.get("status", 404))
                    payload = result_holder.get("payload", {"error": "result_not_ready"})
                self._json_response(status, payload)
                # 최종 화면을 보낸 뒤 상시 서버를 남기지 않고 자동 종료한다.
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if parsed.path == settings.callback_path:
                self._handle_callback(parse_qs(parsed.query))
                return
            self._json_response(404, {"error": "not_found"})

        def _handle_callback(self, query: dict[str, list[str]]) -> None:
            """GitHub callback을 검증하고 token으로 `/user`를 한 번 호출한다."""

            if query.get("error"):
                self._redirect_to_result(400, {"error": "authorization_denied"})
                return

            code = query.get("code", [""])[0]
            state = query.get("state", [""])[0]
            # 등록되지 않았거나 이미 사용했거나 10분이 지난 state는 거부한다.
            code_verifier = pending.consume(state)
            if not code or not code_verifier:
                self._redirect_to_result(400, {"error": "invalid_or_expired_state"})
                return

            try:
                # token은 응답·로그·수집 JSON에 노출하지 않고, Git에서 제외된
                # 로컬 .env에만 저장한다.
                access_token = oauth.exchange_code(code=code, code_verifier=code_verifier)
                client = GitHubClient(access_token)
                response = client.authenticated_user()
                # 이후 별도 callback 서버 없이 수집 코드를 실행할 수 있도록 로컬에 보관한다.
                save_env_value("GITHUB_TOKEN", access_token, settings.env_file)
                collection = collect_repository(client, settings.repository)
                output_path = save_collection(collection, settings.output_dir)
            except GitHubRequestError as error:
                self._redirect_to_result(
                    502, {"error": "github_request_failed", "detail": str(error)}
                )
                return
            except OSError as error:
                self._redirect_to_result(
                    500,
                    {"error": "local_save_failed", "detail": type(error).__name__},
                )
                return

            user = response.data if isinstance(response.data, dict) else {}
            metadata = collection["metadata"]
            self._redirect_to_result(
                200,
                {
                    "authenticated": True,
                    "login": user.get("login"),
                    "user_id": user.get("id"),
                    "rate_limit": response.rate_limit,
                    "repository": settings.repository,
                    "issue_count": metadata["issue_count"],
                    "pull_request_count": metadata["pull_request_count"],
                    "pages_requested": metadata["pages_requested"],
                    "output_file": str(output_path),
                    "message": "OAuth verified, token saved locally, and collection completed.",
                },
            )

        def _redirect_to_result(self, status: int, payload: dict[str, object]) -> None:
            """민감한 callback query를 주소창에서 지우기 위해 `/result`로 이동한다."""

            with result_lock:
                result_holder["status"] = status
                result_holder["payload"] = payload
            self.send_response(303)
            self.send_header("Location", "/result")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def _json_response(self, status: int, payload: dict[str, object]) -> None:
            """캐시되지 않는 UTF-8 JSON HTTP 응답을 작성한다."""

            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            """query string을 제거해 OAuth code/state가 로그에 남지 않게 한다."""

            safe_path = urlsplit(self.path).path
            self.log_message('"%s %s" %s %s', self.command, safe_path, str(code), str(size))

    return OAuthHandler


def main() -> None:
    """환경설정을 읽고 localhost에서 OAuth 검증 서버를 실행한다."""

    settings = OAuthSettings.from_env()
    server = ThreadingHTTPServer(("127.0.0.1", settings.server_port), create_handler(settings))
    print(f"OAuth verification server: http://localhost:{settings.server_port}")
    print(f"Start authorization: http://localhost:{settings.server_port}/login")
    print("Press Ctrl+C to stop. OAuth codes and tokens are not logged.")
    # 실행 환경에 GUI 브라우저가 없더라도 서버 자체는 계속 사용할 수 있다.
    webbrowser.open(f"http://localhost:{settings.server_port}/login")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
