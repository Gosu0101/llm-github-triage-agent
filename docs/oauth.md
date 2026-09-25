# GitHub OAuth 로컬 인증 및 API 호출 검증

관련 Issue: #5

## 범위

외부 웹 프레임워크 없이 Python 표준 라이브러리만 사용한다. 로컬 서버가 OAuth Web Application Flow를 시작하고, callback에서 `state`와 PKCE를 검증한 뒤 발급된 Token으로 `GET /user`를 호출한다. 검증된 Token은 Git에서 제외된 `.env`에 저장하고, 같은 Token으로 Issue·PR 두 페이지를 조회해 `data/oauth/`에 JSON으로 저장한다. 수집이 끝나면 임시 callback 서버는 자동 종료된다.

OAuth App은 과제의 요구사항에 따라 사용한다. 실제 서비스로 확장할 때는 저장소 단위 권한과 짧은 수명의 토큰을 지원하는 GitHub App 전환도 검토한다.

## 사전 설정

GitHub OAuth App 설정과 `.env`의 callback URL은 완전히 같아야 한다.

```dotenv
GITHUB_CLIENT_ID=실제_Client_ID
GITHUB_CLIENT_SECRET=실제_Client_Secret
GITHUB_REDIRECT_URI=http://127.0.0.1:8080/callback
GITHUB_OAUTH_SCOPE=
GITHUB_REPOSITORY=Gosu0101/llm-github-triage-agent
OAUTH_OUTPUT_DIR=data/oauth
```

- 공개 저장소 읽기와 사용자 인증 확인만 수행할 때는 추가 scope를 요청하지 않는다.
- private 저장소 접근처럼 scope가 필요해지면 최소 권한만 요청하고 이유를 문서화한다.
- `.env`는 Git에서 제외되어 있으며 실제 Secret이나 Token을 Issue, PR, 로그, 수집 JSON에 남기지 않는다.
- GitHub OAuth App에 등록한 callback과 `GITHUB_REDIRECT_URI`는 host, port, path까지 정확히 일치해야 한다.

## 실행

Python 3.11 이상에서 외부 패키지 설치 없이 실행할 수 있다.

```bash
PYTHONPATH=src python3 -m triage_agent.oauth_server
```

프로그램이 기본 브라우저에서 `http://localhost:8080/login`을 자동으로 연다. 자동 실행이 되지 않으면 주소를 직접 열어도 된다. 승인이 끝나면 callback 응답에서 다음 값만 확인한다.

callback 처리 후에는 주소창에 일회용 `code`와 `state`가 남지 않도록 query가 없는 `/result`로 이동한다. 화면 공유는 이 최종 결과 화면부터 시작한다.

- `authenticated: true`
- 인증된 GitHub `login`, `user_id`
- `x-ratelimit-*`에서 추출한 Rate Limit 정보
- 조회한 Issue·PR 수와 확인한 페이지 수
- Token이 포함되지 않은 JSON 저장 경로

Access Token은 `.env`의 `GITHUB_TOKEN`에만 저장한다. macOS/Linux에서는 파일 권한을 소유자만 읽고 쓸 수 있는 `0600`으로 제한하고, Windows에서는 POSIX 전용 `os.fchmod()`를 호출하지 않고 저장 폴더의 NTFS ACL을 따른다. Token은 브라우저 응답, 터미널 로그, 수집 JSON에 출력하지 않는다. 수집 결과에도 같은 운영체제별 권한 정책을 적용한다. 인증과 수집이 성공하면 서버가 자동 종료되며, 진행 중 서버가 재시작되면 기존 `state`는 폐기된다.

처음 OAuth를 완료한 뒤에는 callback 서버 없이 저장된 Token으로 다시 수집할 수 있다.

```bash
PYTHONPATH=src python3 -m triage_agent.collect_saved
```

이 명령도 Token은 출력하지 않고 조회 건수, 페이지 수, 새 JSON 경로만 출력한다. Token이 만료되거나 폐기되었다면 OAuth 흐름을 다시 실행한다.

## 보안 처리

- `state`: 32바이트 이상의 암호학적 난수이며 한 번만 사용할 수 있다.
- PKCE: `S256` 방식의 `code_challenge`와 `code_verifier`를 사용한다.
- 만료: callback 대기 상태는 10분 후 폐기한다.
- 로그: callback query string을 기록하지 않아 임시 `code`와 `state`가 노출되지 않는다.
- Token: Git에서 제외된 `.env`에만 원자적으로 저장한다. macOS/Linux에서는 `0600`, Windows에서는 저장 폴더의 NTFS ACL을 적용한다.
- 수집 JSON: Token·Secret 없이 Issue·PR 공개 필드와 Rate Limit만 저장한다.
- 오류: GitHub 오류 응답에서 Secret과 Token이 포함될 수 있는 원문을 그대로 출력하지 않는다.
- 종료: 인증과 수집이 완료되면 localhost callback 서버를 자동 종료한다.

## #4 수집 코드 연결 지점

`triage_agent.github.GitHubClient`가 인증과 수집의 경계이고, `triage_agent.collector`가 실제 두 페이지 조회·분리·JSON 저장을 담당한다. OAuth callback은 같은 client로 `/user`를 검증한 직후 수집 함수를 호출한다. 이후 별도 실행에서도 `.env`의 `GITHUB_TOKEN`으로 client를 만들어 callback 서버 없이 조회할 수 있다.

```python
client = GitHubClient(access_token)
for page in (1, 2):
    issues = client.list_issues("OWNER", "REPO", page=page, per_page=5)
    pulls = client.list_pull_requests("OWNER", "REPO", page=page, per_page=5)
```

각 응답에는 JSON 데이터와 Rate Limit 메타데이터가 함께 들어 있다. `collect_repository()`는 Issues API 결과 중 `pull_request` 키가 있는 항목을 제외하고, PR은 Pulls API에서 상세 정보와 변경 파일을 조회한다. 기본값으로 `per_page=5`를 사용해 Issue·PR endpoint를 각각 2페이지 요청하고 일반 Issue 최대 10건, PR 최대 2건을 저장한다. 페이지 사이에서 같은 ID 또는 번호가 다시 반환되면 한 번만 저장하며, 저장 JSON의 `metadata.per_page`와 `metadata.pages_requested`로 실제 요청 조건을 확인할 수 있다.

## 검증

자동 테스트:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

수동 검증 기록에는 실행 일시, 인증된 계정명, `/user` 성공 여부, Rate Limit 값, 조회 페이지 수, 저장한 Issue/PR 건수와 JSON 경로만 남긴다. 화면 캡처 전에 `.env`, callback query string, Secret·Token이 보이지 않는지 확인한다.

## 참고 자료

- [Authorizing OAuth apps](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps)
- [Authenticating to the REST API with an OAuth app](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authenticating-to-the-rest-api-with-an-oauth-app)
- [REST API rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)
- [REST API endpoints for issues](https://docs.github.com/en/rest/issues/issues)
