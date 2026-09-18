"""저장된 OAuth Token으로 callback 서버 없이 Issue·PR을 다시 수집한다."""

from __future__ import annotations

import os

from .collector import collect_repository, save_collection
from .config import OAuthSettings, load_env_file
from .github import GitHubClient


def main() -> None:
    """`.env`의 Token을 읽어 지정 저장소를 수집하고 결과 경로만 출력한다."""

    load_env_file()
    access_token = os.getenv("GITHUB_TOKEN", "").strip()
    if not access_token:
        raise SystemExit("GITHUB_TOKEN is missing. Run the OAuth flow first.")

    settings = OAuthSettings.from_env()
    payload = collect_repository(GitHubClient(access_token), settings.repository)
    output_path = save_collection(payload, settings.output_dir)
    metadata = payload["metadata"]
    print(
        "Collection completed: "
        f"issues={metadata['issue_count']}, "
        f"pull_requests={metadata['pull_request_count']}, "
        f"pages={metadata['pages_requested']}, "
        f"output={output_path}"
    )


if __name__ == "__main__":
    main()
