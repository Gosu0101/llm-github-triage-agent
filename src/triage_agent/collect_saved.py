"""저장된 OAuth Token으로 분류용 GitHub Issue를 수집한다."""

from __future__ import annotations

import os

from .collector import collect_classification_issues, save_collection
from .config import load_env_file
from .github import GitHubClient


def main() -> None:
    """.env의 Token을 읽어 분류용 Issue를 수집하고 저장한다."""

    load_env_file()

    access_token = os.getenv("GITHUB_TOKEN", "").strip()

    if not access_token:
        raise SystemExit(
            "GITHUB_TOKEN is missing. Run the OAuth flow first."
        )

    payload = collect_classification_issues(
        GitHubClient(access_token),
        "microsoft/vscode",
        per_type=10,
        pages=5,
        per_page=30,
    )

    output_path = save_collection(
        payload,
        r"C:\llm-github-triage-agent\data\oauth",
    )

    print(f"Collection completed: output={output_path}")


if __name__ == "__main__":
    main()