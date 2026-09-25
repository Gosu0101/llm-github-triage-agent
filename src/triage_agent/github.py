"""Python 표준 라이브러리로 구현한 GitHub OAuth 및 REST API client."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


GITHUB_API_VERSION = "2022-11-28"
USER_AGENT = "llm-github-triage-agent/0.1"


class GitHubRequestError(RuntimeError):
    """인증정보를 포함하지 않도록 정리한 GitHub 요청 오류."""


@dataclass(frozen=True)
class GitHubResponse:
    """API JSON 응답과 Rate Limit 정보를 함께 전달하는 반환 객체."""

    data: Any
    rate_limit: dict[str, str | None]


def _rate_limit_headers(headers: Any) -> dict[str, str | None]:
    """HTTP 응답 헤더에서 GitHub Rate Limit 관련 값만 추출한다."""

    return {
        "limit": headers.get("x-ratelimit-limit"),
        "remaining": headers.get("x-ratelimit-remaining"),
        "used": headers.get("x-ratelimit-used"),
        "reset": headers.get("x-ratelimit-reset"),
        "resource": headers.get("x-ratelimit-resource"),
    }


def _json_request(request: Request, *, timeout: float = 15.0) -> tuple[Any, Any]:
    """HTTP 요청을 실행하고 JSON과 응답 헤더를 반환한다.

    GitHub가 반환한 오류 전문에는 민감정보가 포함될 수 있으므로 상태 코드와
    공개 가능한 오류 메시지만 ``GitHubRequestError``로 변환한다.
    """

    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload), response.headers
    except HTTPError as error:
        message = f"GitHub returned HTTP {error.code}"
        try:
            body = json.loads(error.read().decode("utf-8"))
            if isinstance(body, dict) and body.get("error_description"):
                message = f"{message}: {body['error_description']}"
            elif isinstance(body, dict) and body.get("message"):
                message = f"{message}: {body['message']}"
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        raise GitHubRequestError(message) from error
    except (URLError, TimeoutError) as error:
        raise GitHubRequestError("Could not connect to GitHub") from error


class GitHubOAuthClient:
    """Authorization URL 생성과 code-to-token 교환을 담당한다."""

    authorize_endpoint = "https://github.com/login/oauth/authorize"
    token_endpoint = "https://github.com/login/oauth/access_token"

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str, scope: str = ""):
        """OAuth App 설정을 보관한다. Secret은 private 속성으로만 사용한다."""

        self.client_id = client_id
        self._client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.scope = scope

    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        """CSRF 방지용 state와 PKCE challenge가 포함된 승인 URL을 만든다."""

        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        if self.scope:
            params["scope"] = self.scope
        return f"{self.authorize_endpoint}?{urlencode(params)}"

    def exchange_code(self, *, code: str, code_verifier: str) -> str:
        """일회용 authorization code를 OAuth access token으로 교환한다."""

        # GitHub token endpoint가 form 형식을 받으므로 URL encoding한다.
        body = urlencode(
            {
                "client_id": self.client_id,
                "client_secret": self._client_secret,
                "code": code,
                "redirect_uri": self.redirect_uri,
                "code_verifier": code_verifier,
            }
        ).encode("utf-8")
        request = Request(
            self.token_endpoint,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
            },
        )
        payload, _ = _json_request(request)
        if not isinstance(payload, dict) or not payload.get("access_token"):
            error = (
                payload.get("error_description", "Access token was not returned")
                if isinstance(payload, dict)
                else "Invalid token response"
            )
            raise GitHubRequestError(str(error))
        # 이 계층에서는 저장·출력하지 않고 callback 계층에만 전달한다.
        return str(payload["access_token"])


class GitHubClient:
    """#4의 Issue/PR 수집 코드와 공유할 인증된 REST client."""

    api_base = "https://api.github.com"

    def __init__(self, access_token: str):
        """OAuth token을 메모리에 보관한다. 빈 token은 즉시 거부한다."""

        if not access_token:
            raise ValueError("access_token must not be empty")
        self._access_token = access_token

    def get(self, path: str, *, params: dict[str, str | int] | None = None) -> GitHubResponse:
        """GitHub REST API GET 요청을 공통 헤더와 함께 실행한다."""

        query = f"?{urlencode(params)}" if params else ""
        request = Request(
            f"{self.api_base}{path}{query}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._access_token}",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
                "User-Agent": USER_AGENT,
            },
        )
        payload, headers = _json_request(request)
        return GitHubResponse(payload, _rate_limit_headers(headers))

    def authenticated_user(self) -> GitHubResponse:
        """`GET /user`로 token이 유효한지와 인증된 사용자를 확인한다."""

        return self.get("/user")

    def list_issues(
        self,
        owner: str,
        repo: str,
        *,
        page: int = 1,
        per_page: int = 30,
        state: str = "all",
    ) -> GitHubResponse:
        """저장소의 Issue 목록을 페이지 단위로 조회한다.

        GitHub Issues API에는 PR도 포함되므로 반환 항목에 ``pull_request``
        키가 있는지는 #4 수집 단계에서 반드시 확인해야 한다.
        """

        if not 1 <= per_page <= 100:
            raise ValueError("per_page must be between 1 and 100")
        if page < 1:
            raise ValueError("page must be at least 1")
        return self.get(
            f"/repos/{owner}/{repo}/issues",
            params={"state": state, "page": page, "per_page": per_page},
        )

    def list_pull_requests(
        self,
        owner: str,
        repo: str,
        *,
        page: int = 1,
        per_page: int = 30,
        state: str = "all",
    ) -> GitHubResponse:
        """저장소의 PR 목록을 페이지 단위로 별도 조회한다."""

        if not 1 <= per_page <= 100:
            raise ValueError("per_page must be between 1 and 100")
        if page < 1:
            raise ValueError("page must be at least 1")
        return self.get(
            f"/repos/{owner}/{repo}/pulls",
            params={"state": state, "page": page, "per_page": per_page},
        )

    def get_pull_request(self, owner: str, repo: str, number: int) -> GitHubResponse:
        """PR 하나의 additions, deletions, changed_files 등 상세 정보를 조회한다."""

        return self.get(f"/repos/{owner}/{repo}/pulls/{number}")

    def list_pull_request_files(
        self, owner: str, repo: str, number: int, *, per_page: int = 100
    ) -> GitHubResponse:
        """PR 요약 입력에 사용할 변경 파일과 제한된 patch 정보를 조회한다."""

        if not 1 <= per_page <= 100:
            raise ValueError("per_page must be between 1 and 100")
        return self.get(
            f"/repos/{owner}/{repo}/pulls/{number}/files",
            params={"per_page": per_page},
        )
