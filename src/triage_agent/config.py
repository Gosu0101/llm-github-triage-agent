"""로컬 OAuth 검증 서버가 사용할 환경변수를 읽고 검증한다."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
from urllib.parse import urlparse


# Windows에는 os.fchmod가 없고 POSIX의 0600 권한 비트도 동일하게 적용되지
# 않는다. Windows에서는 파일이 위치한 폴더의 NTFS ACL을 따르게 하고,
# macOS/Linux에서만 소유자 전용 권한을 명시적으로 설정한다.
_IS_WINDOWS = os.name == "nt"


def load_env_file(path: str | Path = ".env") -> None:
    """간단한 ``KEY=VALUE`` 형식의 파일을 환경변수로 불러온다.

    python-dotenv 같은 외부 패키지를 사용하지 않기 위한 최소 파서다. 이미
    운영체제에 설정된 환경변수는 덮어쓰지 않으며, 일반 값과 따옴표로 감싼
    값을 지원한다.
    """

    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        # 빈 줄, 주석, 잘못된 줄은 무시한다.
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            # 셸/CI가 제공한 값이 .env보다 우선한다.
            os.environ.setdefault(key, value)


def save_env_value(key: str, value: str, path: str | Path = ".env") -> None:
    """민감한 값을 `.env`에 원자적으로 저장하고 접근 권한을 제한한다.

    기존 키가 있으면 값을 교체하고, 없으면 마지막에 추가한다. 임시 파일에
    완성본을 쓴 뒤 ``os.replace``하므로 쓰기 도중 프로세스가 중단돼도 기존
    `.env`가 반쯤 써진 상태로 남을 가능성을 줄인다. 값은 반환하거나 출력하지
    않는다. macOS/Linux에서는 권한을 0600으로 지정하며 Windows에서는
    ``os.fchmod`` 대신 해당 폴더의 NTFS ACL을 따른다.
    """

    if not key or "=" in key or "\n" in key:
        raise ValueError("Invalid environment variable name")
    if "\n" in value or "\r" in value:
        raise ValueError("Environment variable value must be a single line")

    env_path = Path(path)
    original_lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    updated_lines: list[str] = []
    replaced = False
    for line in original_lines:
        if line.split("=", 1)[0].strip() == key and "=" in line:
            updated_lines.append(f"{key}={value}")
            replaced = True
        else:
            updated_lines.append(line)
    if not replaced:
        updated_lines.append(f"{key}={value}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{env_path.name}.", dir=env_path.parent, text=True
    )
    try:
        if not _IS_WINDOWS:
            os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write("\n".join(updated_lines) + "\n")
        os.replace(temporary_name, env_path)
        if not _IS_WINDOWS:
            os.chmod(env_path, 0o600)
    except Exception:
        try:
            os.close(file_descriptor)
        except OSError:
            pass
        Path(temporary_name).unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class OAuthSettings:
    """GitHub OAuth 실행에 필요한 설정 값 묶음.

    ``frozen=True``이므로 생성 후 실수로 Client ID나 callback 주소를
    변경할 수 없다. Client Secret 자체를 출력하는 기능은 제공하지 않는다.
    """

    client_id: str
    client_secret: str
    redirect_uri: str
    scope: str = ""
    repository: str = "Gosu0101/llm-github-triage-agent"
    output_dir: Path = Path("data/oauth")
    env_file: Path = Path(".env")

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> "OAuthSettings":
        """`.env`와 운영체제 환경변수에서 설정을 읽고 즉시 검증한다."""

        load_env_file(env_file)
        settings = cls(
            client_id=os.getenv("GITHUB_CLIENT_ID", "").strip(),
            client_secret=os.getenv("GITHUB_CLIENT_SECRET", "").strip(),
            redirect_uri=os.getenv("GITHUB_REDIRECT_URI", "").strip(),
            scope=os.getenv("GITHUB_OAUTH_SCOPE", "").strip(),
            repository=os.getenv(
                "GITHUB_REPOSITORY", "Gosu0101/llm-github-triage-agent"
            ).strip(),
            output_dir=Path(os.getenv("OAUTH_OUTPUT_DIR", "data/oauth").strip()),
            env_file=Path(env_file),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        """필수 값과 로컬 검증 서버에서 허용하는 callback인지 확인한다."""

        missing = [
            name
            for name, value in (
                ("GITHUB_CLIENT_ID", self.client_id),
                ("GITHUB_CLIENT_SECRET", self.client_secret),
                ("GITHUB_REDIRECT_URI", self.redirect_uri),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        # 이 구현은 로컬 실습용 HTTP 서버이므로 외부 호스트에 바인딩하지 않는다.
        parsed = urlparse(self.redirect_uri)
        if parsed.scheme != "http":
            raise ValueError("The local verification server requires an http redirect URI")
        if parsed.hostname not in {"localhost", "127.0.0.1"}:
            raise ValueError("The local verification server only binds to localhost")
        if not parsed.path.startswith("/"):
            raise ValueError("GITHUB_REDIRECT_URI must include a callback path")
        repository_parts = self.repository.split("/")
        if len(repository_parts) != 2 or not all(repository_parts):
            raise ValueError("GITHUB_REPOSITORY must use OWNER/REPO format")

    @property
    def callback_path(self) -> str:
        """등록된 redirect URI에서 `/callback` 같은 경로만 반환한다."""

        return urlparse(self.redirect_uri).path

    @property
    def server_port(self) -> int:
        """redirect URI의 포트를 반환하고, 생략된 경우 8080을 사용한다."""

        return urlparse(self.redirect_uri).port or 8080
