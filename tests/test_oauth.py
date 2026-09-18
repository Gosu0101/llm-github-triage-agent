from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from triage_agent.collector import collect_repository, save_collection
from triage_agent.config import OAuthSettings, load_env_file, save_env_value
from triage_agent.github import GitHubClient, GitHubOAuthClient, GitHubResponse
from triage_agent.oauth_server import PendingAuthorizationStore, _pkce_challenge


class OAuthTests(unittest.TestCase):
    """OAuth 보안 규칙과 입력 검증을 외부 네트워크 없이 확인한다."""

    def test_pkce_challenge_is_base64url_without_padding(self) -> None:
        """PKCE challenge가 URL에 안전하고 padding이 없는지 확인한다."""

        challenge = _pkce_challenge("a" * 64)
        self.assertNotIn("=", challenge)
        self.assertNotIn("+", challenge)
        self.assertNotIn("/", challenge)

    def test_state_is_one_use(self) -> None:
        """같은 state를 두 번 사용할 수 없는지 확인한다."""

        store = PendingAuthorizationStore()
        state, verifier, _ = store.create()
        self.assertEqual(store.consume(state), verifier)
        self.assertIsNone(store.consume(state))

    def test_expired_state_is_rejected(self) -> None:
        """TTL이 지난 승인 요청이 거부되는지 확인한다."""

        now = [0.0]
        store = PendingAuthorizationStore(ttl_seconds=10, clock=lambda: now[0])
        state, _, _ = store.create()
        now[0] = 11.0
        self.assertIsNone(store.consume(state))

    def test_authorization_url_contains_state_and_pkce(self) -> None:
        """GitHub 승인 URL에 CSRF와 PKCE 보호 값이 포함되는지 확인한다."""

        client = GitHubOAuthClient("client", "secret", "http://localhost:8000/callback")
        url = client.authorization_url(state="state", code_challenge="challenge")
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["state"], ["state"])
        self.assertEqual(query["code_challenge"], ["challenge"])
        self.assertEqual(query["code_challenge_method"], ["S256"])

    def test_empty_scope_is_not_requested(self) -> None:
        """불필요한 GitHub 권한을 기본으로 요청하지 않는지 확인한다."""

        client = GitHubOAuthClient("client", "secret", "http://localhost:8000/callback")
        query = parse_qs(urlsplit(client.authorization_url(state="s", code_challenge="c")).query)
        self.assertNotIn("scope", query)

    def test_settings_restrict_server_to_localhost(self) -> None:
        """실습 서버가 외부 호스트 callback을 허용하지 않는지 확인한다."""

        settings = OAuthSettings("id", "secret", "http://example.com/callback")
        with self.assertRaisesRegex(ValueError, "localhost"):
            settings.validate()

    def test_env_file_does_not_override_existing_environment(self) -> None:
        """CI·셸 환경변수가 `.env`보다 우선하는지 확인한다."""

        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("TEST_OAUTH_VALUE=file\n", encoding="utf-8")
            with patch.dict(os.environ, {"TEST_OAUTH_VALUE": "environment"}, clear=False):
                load_env_file(env_file)
                self.assertEqual(os.environ["TEST_OAUTH_VALUE"], "environment")

    def test_pagination_arguments_are_validated_before_request(self) -> None:
        """잘못된 pagination 입력을 네트워크 요청 전에 막는지 확인한다."""

        client = GitHubClient("not-a-real-token")
        with self.assertRaisesRegex(ValueError, "per_page"):
            client.list_issues("owner", "repo", per_page=101)
        with self.assertRaisesRegex(ValueError, "page"):
            client.list_pull_requests("owner", "repo", page=0)

    def test_token_is_replaced_in_env_without_exposing_other_values(self) -> None:
        """Token 저장 시 기존 설정을 보존하고 해당 키만 교체하는지 확인한다."""

        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("GITHUB_CLIENT_ID=id\nGITHUB_TOKEN=old\n", encoding="utf-8")
            save_env_value("GITHUB_TOKEN", "new-token", env_file)
            contents = env_file.read_text(encoding="utf-8")
            self.assertIn("GITHUB_CLIENT_ID=id", contents)
            self.assertIn("GITHUB_TOKEN=new-token", contents)
            self.assertNotIn("GITHUB_TOKEN=old", contents)
            if os.name != "nt":
                self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)

    def test_env_save_does_not_call_fchmod_on_windows(self) -> None:
        """Windows 환경에서는 존재하지 않는 os.fchmod를 호출하지 않는다."""

        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            with (
                patch("triage_agent.config._IS_WINDOWS", True),
                patch.object(
                    os, "fchmod", side_effect=AssertionError("fchmod called"), create=True
                ),
            ):
                save_env_value("GITHUB_TOKEN", "token", env_file)
            self.assertEqual(env_file.read_text(encoding="utf-8"), "GITHUB_TOKEN=token\n")

    def test_collection_separates_issues_and_requests_two_pages(self) -> None:
        """두 페이지의 서로 다른 항목을 중복·누락 없이 합치는지 확인한다."""

        class FakeClient:
            def __init__(self) -> None:
                self.issue_requests: list[tuple[int, int]] = []
                self.pull_requests: list[tuple[int, int]] = []
                self.detail_numbers: list[int] = []

            def list_issues(self, owner, repo, *, page, per_page):
                self.issue_requests.append((page, per_page))
                items = {
                    1: [
                        {"id": 1, "number": 1, "title": "Issue 1", "labels": []},
                        {"id": 2, "number": 2, "title": "Issue 2", "labels": []},
                    ],
                    2: [
                        # 페이지 경계가 변해 같은 항목이 다시 와도 한 번만 저장한다.
                        {"id": 2, "number": 2, "title": "Issue 2", "labels": []},
                        {"id": 3, "number": 3, "title": "Issue 3", "labels": []},
                        {"id": 103, "number": 103, "pull_request": {"url": "example"}},
                    ],
                }
                return GitHubResponse(
                    items[page],
                    {"remaining": "4999"},
                )

            def list_pull_requests(self, owner, repo, *, page, per_page, state="all"):
                self.pull_requests.append((page, per_page))
                items = {
                    1: [
                        {"id": 10, "number": 10, "title": "PR 10", "user": {}, "base": {}, "head": {}}
                    ],
                    2: [
                        {"id": 10, "number": 10, "title": "PR 10", "user": {}, "base": {}, "head": {}},
                        {"id": 11, "number": 11, "title": "PR 11", "user": {}, "base": {}, "head": {}},
                    ],
                }
                return GitHubResponse(
                    items[page],
                    {"remaining": "4998"},
                )

            def get_pull_request(self, owner, repo, number):
                self.detail_numbers.append(number)
                return GitHubResponse({"changed_files": 1}, {"remaining": "4997"})

            def list_pull_request_files(self, owner, repo, number):
                return GitHubResponse([{"filename": "example.py", "patch": "+pass"}], {"remaining": "4996"})

        client = FakeClient()
        result = collect_repository(
            client,
            "owner/repo",
            issue_limit=10,
            pull_request_limit=2,
            pages=2,
            per_page=2,
        )
        self.assertEqual(client.issue_requests, [(1, 2), (2, 2)])
        self.assertEqual(client.pull_requests, [(1, 2), (2, 2)])
        self.assertEqual([item["number"] for item in result["issues"]], [1, 2, 3])
        self.assertEqual([item["number"] for item in result["pull_requests"]], [10, 11])
        self.assertEqual(client.detail_numbers, [10, 11])
        self.assertEqual(result["metadata"]["per_page"], 2)

    def test_collection_pagination_is_validated_before_request(self) -> None:
        """잘못된 페이지 설정은 GitHub 요청 전에 거부한다."""

        class NoRequestClient:
            def list_issues(self, *args, **kwargs):
                raise AssertionError("network request should not be made")

        with self.assertRaisesRegex(ValueError, "per_page"):
            collect_repository(NoRequestClient(), "owner/repo", per_page=0)
        with self.assertRaisesRegex(ValueError, "pages"):
            collect_repository(NoRequestClient(), "owner/repo", pages=0)

    def test_collection_json_does_not_contain_token(self) -> None:
        """저장 JSON에 인증 Token이 들어가지 않고 권한이 0600인지 확인한다."""

        with tempfile.TemporaryDirectory() as directory:
            output = save_collection({"metadata": {}, "issues": [], "pull_requests": []}, directory)
            self.assertNotIn("token", output.read_text(encoding="utf-8").lower())
            if os.name != "nt":
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_collection_save_does_not_call_fchmod_on_windows(self) -> None:
        """Windows 환경에서도 수집 JSON을 os.fchmod 오류 없이 저장한다."""

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("triage_agent.collector._IS_WINDOWS", True),
                patch.object(
                    os, "fchmod", side_effect=AssertionError("fchmod called"), create=True
                ),
            ):
                output = save_collection(
                    {"metadata": {}, "issues": [], "pull_requests": []}, directory
                )
            self.assertTrue(output.exists())


if __name__ == "__main__":
    unittest.main()
