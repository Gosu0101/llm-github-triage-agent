"""OAuth Token으로 Issue·PR을 조회하고 안전한 로컬 JSON으로 저장한다."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .github import GitHubClient


# Windows에는 POSIX 전용 os.fchmod가 없다. Windows 저장 파일은 NTFS ACL을
# 따르고, macOS/Linux에서만 소유자 전용 0600 권한을 명시적으로 적용한다.
_IS_WINDOWS = os.name == "nt"


def _labels(item: dict[str, Any]) -> list[str]:
    """GitHub label 객체 목록에서 label 이름만 추출한다."""

    return [
        str(label.get("name"))
        for label in item.get("labels", [])
        if isinstance(label, dict) and label.get("name")
    ]


def _normalize_issue(item: dict[str, Any]) -> dict[str, Any]:
    """중복 탐지와 평가에 필요한 공개 Issue 필드만 선택한다."""

    return {
        "id": item.get("id"),
        "number": item.get("number"),
        "title": item.get("title"),
        "body": item.get("body"),
        "labels": _labels(item),
        "state": item.get("state"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "html_url": item.get("html_url"),
        "comments_count": item.get("comments"),
    }


def _normalize_pull_request(
    item: dict[str, Any], detail: dict[str, Any], files: list[dict[str, Any]]
) -> dict[str, Any]:
    """PR 요약에 필요한 메타데이터와 변경 파일을 하나의 객체로 정리한다."""

    normalized_files = []
    for changed_file in files:
        # 지나치게 큰 patch가 JSON과 이후 LLM 입력을 차지하지 않도록 제한한다.
        patch = changed_file.get("patch")
        normalized_files.append(
            {
                "filename": changed_file.get("filename"),
                "status": changed_file.get("status"),
                "additions": changed_file.get("additions"),
                "deletions": changed_file.get("deletions"),
                "changes": changed_file.get("changes"),
                "patch": patch[:12_000] if isinstance(patch, str) else None,
                "patch_truncated": isinstance(patch, str) and len(patch) > 12_000,
            }
        )

    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    base = item.get("base") if isinstance(item.get("base"), dict) else {}
    head = item.get("head") if isinstance(item.get("head"), dict) else {}
    return {
        "id": item.get("id"),
        "number": item.get("number"),
        "title": item.get("title"),
        "body": item.get("body"),
        "user_login": user.get("login"),
        "base_ref": base.get("ref"),
        "head_ref": head.get("ref"),
        "state": item.get("state"),
        "merged_at": detail.get("merged_at"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "html_url": item.get("html_url"),
        "changed_files": detail.get("changed_files"),
        "additions": detail.get("additions"),
        "deletions": detail.get("deletions"),
        "files": normalized_files,
    }


def collect_repository(
    client: GitHubClient,
    repository: str,
    *,
    issue_limit: int = 10,
    pull_request_limit: int = 2,
    pages: int = 2,
    per_page: int = 5,
) -> dict[str, Any]:
    """여러 페이지를 조회해 일반 Issue와 PR을 분리하고 정규화한다.

    Issues endpoint는 PR도 섞어 반환하므로 ``pull_request`` 키가 없는 항목만
    일반 Issue로 취급한다. PR은 Pulls endpoint에서 다시 조회한 뒤 상세 정보와
    변경 파일을 추가한다. 작은 ``per_page`` 기본값으로 실제 데이터가 여러
    페이지에 나뉘게 하고, 페이지 사이에 같은 항목이 나타나도 ID 또는 번호를
    기준으로 한 번만 저장한다.
    """

    if pages < 1:
        raise ValueError("pages must be at least 1")
    if not 1 <= per_page <= 100:
        raise ValueError("per_page must be between 1 and 100")
    if issue_limit < 0 or pull_request_limit < 0:
        raise ValueError("collection limits must not be negative")

    owner, repo = repository.split("/", 1)
    issues: list[dict[str, Any]] = []
    pull_request_items: list[dict[str, Any]] = []
    seen_issue_keys: set[tuple[str, str]] = set()
    seen_pull_request_keys: set[tuple[str, str]] = set()
    rate_limits: dict[str, dict[str, str | None]] = {}

    for page in range(1, pages + 1):
        response = client.list_issues(owner, repo, page=page, per_page=per_page)
        rate_limits["issues"] = response.rate_limit
        for item in response.data if isinstance(response.data, list) else []:
            if not isinstance(item, dict):
                continue
            if "pull_request" in item:
                continue
            # GitHub 응답에는 id와 number가 있지만, 테스트 대역처럼 id가 없는
            # 경우에도 number로 페이지 간 중복을 판별한다.
            key_name = "id" if item.get("id") is not None else "number"
            key_value = item.get(key_name)
            if key_value is None:
                continue
            item_key = (key_name, str(key_value))
            if item_key not in seen_issue_keys and len(issues) < issue_limit:
                seen_issue_keys.add(item_key)
                issues.append(_normalize_issue(item))

        response = client.list_pull_requests(owner, repo, page=page, per_page=per_page)
        rate_limits["pull_requests"] = response.rate_limit
        for item in response.data if isinstance(response.data, list) else []:
            if not isinstance(item, dict):
                continue
            key_name = "id" if item.get("id") is not None else "number"
            key_value = item.get(key_name)
            if key_value is None:
                continue
            item_key = (key_name, str(key_value))
            if (
                item_key not in seen_pull_request_keys
                and len(pull_request_items) < pull_request_limit
            ):
                seen_pull_request_keys.add(item_key)
                pull_request_items.append(item)

    pull_requests: list[dict[str, Any]] = []
    for item in pull_request_items:
        number = item.get("number")
        if not isinstance(number, int):
            continue
        detail_response = client.get_pull_request(owner, repo, number)
        files_response = client.list_pull_request_files(owner, repo, number)
        rate_limits["pull_request_details"] = files_response.rate_limit
        detail = detail_response.data if isinstance(detail_response.data, dict) else {}
        files = files_response.data if isinstance(files_response.data, list) else []
        pull_requests.append(_normalize_pull_request(item, detail, files))

    return {
        "metadata": {
            "repository": repository,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "pages_requested": pages,
            "per_page": per_page,
            "issue_count": len(issues),
            "pull_request_count": len(pull_requests),
            "rate_limits": rate_limits,
        },
        "issues": issues,
        "pull_requests": pull_requests,
    }


def save_collection(payload: dict[str, Any], output_dir: str | Path) -> Path:
    """수집 결과를 Token 없이 timestamp가 붙은 비공개 JSON으로 저장한다.

    macOS/Linux에서는 파일 권한을 0600으로 제한하며 Windows에서는 저장
    폴더의 NTFS ACL을 따른다.
    """

    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = directory / f"github-collection-{timestamp}.json"
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".collection-", suffix=".json", dir=directory, text=True
    )
    try:
        if not _IS_WINDOWS:
            os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
            json.dump(payload, temporary_file, ensure_ascii=False, indent=2)
            temporary_file.write("\n")
        os.replace(temporary_name, destination)
        if not _IS_WINDOWS:
            os.chmod(destination, 0o600)
    except Exception:
        try:
            os.close(file_descriptor)
        except OSError:
            pass
        Path(temporary_name).unlink(missing_ok=True)
        raise
    return destination
