# 통합 데이터 스키마 초안

관련 Issue: #4, #5, #8

상태: 초안 — 사용 저장소·공통 라벨 매핑·라벨 충돌 처리 규칙은 회의에서 확정

## 1. 목적

1차시에서 각 담당자가 준비한 결과를 하나의 데이터 흐름으로 연결한다.

```text
GitHub 데이터 수집
        ↓
기능별 모델 입력 생성
        ↓
Agent 결과 생성
        ↓
평가용 정답과 비교
```

각 담당 결과는 중복 작업이 아니라 서로 다른 계층이다.

| 담당 | 정의하는 내용 |
|---|---|
| 여윤성 | 유형 분류·우선순위 등 기능의 입력과 출력 |
| 강세웅 | 중복 탐지·PR 요약에 필요한 데이터 |
| 이형언 | 정답 데이터, 라벨 변환, 데이터 분리, 평가지표 |
| 전체 | 2차시에서 공통 수집 필드와 최종 스키마 확정 |

OAuth는 이 스키마의 데이터 공급 전에 위치한다. 인증된 Token을 GitHub API
Client에 전달할 뿐, 모델 입력이나 평가 정답에는 포함하지 않는다.

## 2. 반드시 구분할 데이터 계층

```text
수집 데이터
├── 기능별 모델 입력
└── 평가용 정답 데이터
```

수집한 모든 필드를 모델에 전달하지 않는다. `number`, `html_url` 등은 원본
추적에 필요하지만 유형 분류의 의미 입력은 아니다. `original_labels`, 명시적인
duplicate 링크와 사후 maintainer 댓글은 정답 유출을 막기 위해 모델 입력에서
제외한다.

## 3. 공통 수집 데이터

### 3.1 Issue 공통 필드

| 필드 | 용도 | 모델 입력 여부 |
|---|---|---:|
| `repository` | 저장소 구분 및 중복 후보 필터링 | 필터링 전용 |
| `id` | GitHub 내부 고유 식별자 | 아니요 |
| `number` | Issue 식별 및 결과 연결 | 식별 전용 |
| `title` | 분류·우선순위·중복 탐지 | 예 |
| `body` | 분류·우선순위·중복 탐지 | 예 |
| `state` | 원본 상태 기록 | 기본 제외 |
| `created_at` | 이전 Issue만 중복 후보로 선택 | 필터링 전용 |
| `updated_at` | 데이터 수집 시점 확인 | 아니요 |
| `html_url` | 사람이 원본 확인 | 아니요 |
| `is_pull_request` | 일반 Issue와 PR 구분 | 필터링 전용 |
| `original_labels` | 평가 정답 생성 | 절대 제외 |

목표 Issue 레코드 예시:

```json
{
  "repository": "OWNER/REPO",
  "id": 123456,
  "number": 15,
  "title": "OAuth callback fails",
  "body": "state mismatch...",
  "state": "open",
  "created_at": "2026-09-18T00:00:00Z",
  "updated_at": "2026-09-18T01:00:00Z",
  "html_url": "https://github.com/OWNER/REPO/issues/15",
  "is_pull_request": false,
  "original_labels": ["bug"]
}
```

### 3.2 PR 추가 필드

| 필드 | 용도 |
|---|---|
| `base_ref` | 병합 대상 브랜치 |
| `head_ref` | 변경 브랜치 |
| `merged_at` | 병합 여부와 시점 |
| `changed_files_count` | 변경 파일 개수 |
| `additions` | 추가 줄 수 |
| `deletions` | 삭제 줄 수 |
| `files[]` | 파일 경로, 상태, 변경 통계, 제한된 patch |
| `commit_messages` | 보조 입력, 필요성 확인 후 선택 수집 |

`changed_files`라는 이름은 파일 개수와 파일 목록 중 무엇인지 모호하므로 목표
스키마에서는 `changed_files_count`와 `files[]`로 구분한다.

목표 PR 레코드 예시:

```json
{
  "repository": "OWNER/REPO",
  "id": 654321,
  "number": 21,
  "title": "Add OAuth callback handler",
  "body": "Implements code and state handling.",
  "state": "open",
  "html_url": "https://github.com/OWNER/REPO/pull/21",
  "is_pull_request": true,
  "base_ref": "main",
  "head_ref": "feature/oauth",
  "merged_at": null,
  "changed_files_count": 2,
  "additions": 30,
  "deletions": 5,
  "files": [
    {
      "filename": "src/oauth.py",
      "status": "modified",
      "patch": "+callback handling",
      "patch_truncated": false
    }
  ]
}
```

## 4. 현재 수집 코드와 목표 스키마의 차이

현재 `triage_agent.collector.collect_repository()`는 OAuth 검증 후 실제 GitHub
데이터를 수집하며 Issue와 PR을 별도 배열로 반환한다.

### 4.1 구현 파일 전체 위치

프로젝트 루트를 기준으로 현재 구현은 다음 파일에 있다.

| 파일 | 구현 내용 |
|---|---|
| `src/triage_agent/config.py` | `.env` 로드, OAuth 설정 검증, Token 안전 저장 |
| `src/triage_agent/github.py` | OAuth code 교환, 인증된 GitHub REST API 호출 |
| `src/triage_agent/oauth_server.py` | 로그인·callback·Token 검증·최초 수집 연결 |
| `src/triage_agent/collector.py` | Issue·PR 정규화, Pagination, 중복 제거, JSON 저장 |
| `src/triage_agent/collect_saved.py` | 저장된 Token으로 callback 없이 재수집 |
| `tests/test_oauth.py` | OAuth·Pagination·저장 보안·Windows 호환 테스트 |

### 4.2 OAuth 설정과 Token 저장

구현 파일: `src/triage_agent/config.py`

| 위치 | 함수·클래스 | 구현 내용 |
|---:|---|---|
| 18 | `load_env_file()` | `.env`의 `KEY=VALUE`를 읽고 기존 셸 환경변수는 덮어쓰지 않음 |
| 45 | `save_env_value()` | Token을 임시 파일에 쓴 뒤 `os.replace()`로 원자적 저장 |
| 78~84 | 운영체제별 권한 처리 | macOS/Linux는 `0600`, Windows는 NTFS ACL 사용 |
| 95 | `OAuthSettings` | Client ID·Secret·redirect URI·저장소·출력 경로 관리 |
| 111 | `OAuthSettings.from_env()` | `.env`와 환경변수를 설정 객체로 변환 |
| 129 | `OAuthSettings.validate()` | 필수 값, localhost callback, `OWNER/REPO` 형식 검증 |

Token은 프로젝트 루트의 `.env`에 `GITHUB_TOKEN`으로 저장된다. `.env`는
`.gitignore`로 제외되며 수집 JSON이나 브라우저 결과에는 포함되지 않는다.

### 4.3 GitHub OAuth와 REST API Client

구현 파일: `src/triage_agent/github.py`

| 위치 | 함수·클래스 | 구현 내용 |
|---:|---|---|
| 29 | `_rate_limit_headers()` | GitHub 응답에서 Rate Limit 헤더 추출 |
| 41 | `_json_request()` | 공통 HTTP 요청, JSON 변환, timeout·오류 처리 |
| 67 | `GitHubOAuthClient` | GitHub OAuth App 설정 관리 |
| 81 | `authorization_url()` | `state`와 PKCE challenge가 포함된 승인 URL 생성 |
| 95 | `exchange_code()` | Authorization Code를 Access Token으로 교환 |
| 130 | `GitHubClient` | Access Token을 사용하는 REST API Client |
| 142 | `GitHubClient.get()` | 인증·API 버전·User-Agent 헤더를 포함한 GET 요청 |
| 158 | `authenticated_user()` | `GET /user`로 Token과 인증 사용자 확인 |
| 163 | `list_issues()` | `page`, `per_page`, `state`로 Issue 조회 |
| 187 | `list_pull_requests()` | PR 목록을 페이지 단위로 조회 |
| 207 | `get_pull_request()` | PR 변경 통계와 병합 정보 조회 |
| 212 | `list_pull_request_files()` | PR 변경 파일과 patch 조회 |

### 4.4 Issue 공통 필드 구현

구현 파일: `src/triage_agent/collector.py`

`_labels()`는 20번째 줄에서 GitHub label 객체의 이름만 추출한다.
`_normalize_issue()`는 30번째 줄에서 원본 Issue를 다음 구조로 정리한다.

```python
{
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
```

| 목표 필드 | 현재 코드 위치 | 상태 |
|---|---|---|
| `repository` | `collect_repository()`의 `metadata.repository` | 부분 구현: 레코드가 아닌 metadata에 한 번 저장 |
| `id` | `_normalize_issue()` | 구현 |
| `number` | `_normalize_issue()` | 구현 |
| `title` | `_normalize_issue()` | 구현 |
| `body` | `_normalize_issue()` | 구현 |
| `state` | `_normalize_issue()` | 구현 |
| `created_at` | `_normalize_issue()` | 구현 |
| `updated_at` | `_normalize_issue()` | 구현 |
| `html_url` | `_normalize_issue()` | 구현 |
| `original_labels` | `_normalize_issue()`의 `labels` | 데이터는 수집되며 필드명만 다름 |
| `is_pull_request` | `issues[]`, `pull_requests[]` 배열 분리 | 명시적 필드는 없지만 의미상 구현 |
| Issue 댓글 본문 | 없음 | 미구현, 현재는 `comments_count`만 수집 |

### 4.5 PR 추가 필드 구현

구현 파일: `src/triage_agent/collector.py`

`_normalize_pull_request()`는 47번째 줄에서 PR 목록·상세 정보·변경 파일을
하나의 레코드로 합친다.

| 목표 필드 | 현재 코드 위치 | 상태 |
|---|---|---|
| `base_ref` | `_normalize_pull_request()` | 구현 |
| `head_ref` | `_normalize_pull_request()` | 구현 |
| `merged_at` | `_normalize_pull_request()` | 구현 |
| `changed_files_count` | 현재 `changed_files` | 데이터는 수집되며 필드명만 다름 |
| `additions` | `_normalize_pull_request()` | 구현 |
| `deletions` | `_normalize_pull_request()` | 구현 |
| `files[]` | `_normalize_pull_request()` | 구현 |
| `files[].filename` | 변경 파일 정규화 | 구현 |
| `files[].status` | 변경 파일 정규화 | 구현 |
| `files[].patch` | 변경 파일 정규화 | 구현, 파일별 최대 12,000자 |
| `files[].patch_truncated` | 변경 파일 정규화 | 구현 |
| `commit_messages` | 관련 API 호출 없음 | 미구현 |
| CI 결과 | 관련 API 호출 없음 | 미구현 |

### 4.6 Pagination, Issue·PR 분리, 중복 제거

구현 파일: `src/triage_agent/collector.py`

핵심 함수는 91번째 줄의 `collect_repository()`다.

```python
collect_repository(
    client,
    repository,
    issue_limit=10,
    pull_request_limit=2,
    pages=2,
    per_page=5,
)
```

처리 순서:

1. `pages`, `per_page`, 저장 개수를 API 요청 전에 검증한다.
2. Issue와 PR endpoint를 각각 `pages=2`, `per_page=5`로 요청한다.
3. Issues API 응답에서 `pull_request` 키가 있는 항목을 제외한다.
4. Issue와 PR의 `id` 또는 `number`를 기록한다.
5. 다른 페이지에서 같은 항목이 다시 오면 한 번만 저장한다.
6. 선택된 PR의 상세 정보와 변경 파일 endpoint를 추가 호출한다.
7. `metadata`, `issues`, `pull_requests`를 하나의 `dict`로 반환한다.

여기서 구현된 중복 제거는 Pagination에서 같은 GitHub 항목이 반복되는 것을
막는 기능이다. 의미가 비슷한 중복 Issue를 탐지하는 기능은 아직 구현되지 않았다.

반환 구조:

```json
{
  "metadata": {
    "repository": "OWNER/REPO",
    "collected_at": "UTC timestamp",
    "pages_requested": 2,
    "per_page": 5,
    "issue_count": 8,
    "pull_request_count": 2,
    "rate_limits": {}
  },
  "issues": [],
  "pull_requests": []
}
```

### 4.7 수집 JSON 저장

구현 파일: `src/triage_agent/collector.py`

186번째 줄의 `save_collection()`이 수집 결과를 다음 위치에 저장한다.

```text
data/oauth/github-collection-<UTC timestamp>.json
```

- Token과 Client Secret을 payload에 추가하지 않는다.
- 임시 파일 작성 후 `os.replace()`로 최종 파일을 교체한다.
- macOS/Linux에서는 파일 권한을 `0600`으로 제한한다.
- Windows에서는 `os.fchmod()`를 호출하지 않고 NTFS ACL을 따른다.
- `data/`는 Git에 커밋하지 않는다.

### 4.8 OAuth callback과 수집 코드 연결

구현 파일: `src/triage_agent/oauth_server.py`

| 위치 | 구현 내용 |
|---:|---|
| 22 | `_pkce_challenge()`로 PKCE S256 challenge 생성 |
| 37 | `PendingAuthorizationStore`에서 state·verifier·10분 만료 관리 |
| 86 | `create_handler()`에서 `/login`, `/callback`, `/result` 처리 |
| 161 | `/user` 호출로 발급된 Token 검증 |
| 163 | `save_env_value()`로 Token을 `.env`에 저장 |
| 164 | `collect_repository()`로 Issue·PR 수집 |
| 165 | `save_collection()`으로 JSON 저장 |
| 228 | `main()`에서 localhost 임시 서버 실행 |

핵심 연결 코드:

```python
response = client.authenticated_user()
save_env_value("GITHUB_TOKEN", access_token, settings.env_file)
collection = collect_repository(client, settings.repository)
output_path = save_collection(collection, settings.output_dir)
```

### 4.9 저장된 Token으로 재수집

구현 파일: `src/triage_agent/collect_saved.py`

12번째 줄의 `main()`이 `.env`의 `GITHUB_TOKEN`을 읽고, 21번째 줄에서 같은
`collect_repository()`를 호출한다.

```bash
PYTHONPATH=src python3 -m triage_agent.collect_saved
```

### 4.10 구현 검증 테스트

구현 파일: `tests/test_oauth.py`

| 위치 | 검증 내용 |
|---:|---|
| 19 | PKCE challenge 형식 |
| 27 | state 일회성 사용 |
| 35 | 만료된 state 거부 |
| 44 | 승인 URL의 state·PKCE 포함 여부 |
| 61 | localhost callback 제한 |
| 87 | Token 저장과 기존 `.env` 값 보존 |
| 101 | Windows에서 `os.fchmod()` 미호출 |
| 115 | 두 페이지 병합·Issue/PR 분리·페이지 중복 제거 |
| 182 | 잘못된 Pagination 설정을 요청 전에 차단 |
| 194 | 수집 JSON에 Token 미포함 및 POSIX `0600` |
| 203 | Windows JSON 저장 호환성 |

현재 자동 테스트는 총 14개다.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

### 4.11 아직 구현되지 않은 부분

다음 항목은 이 문서에 목표로 정의되어 있지만 현재 Python 구현은 없다.

| 미구현 기능 | 필요한 후속 작업 |
|---|---|
| 기능별 모델 입력 생성 | 수집 레코드에서 필요한 필드만 선택하는 변환기 |
| 유형 분류 | 모델 호출과 `type`, `rationale` 결과 검증 |
| 우선순위 추정 | 모델 호출과 `priority`, `rationale` 결과 검증 |
| 중복 후보 필터 | 같은 저장소·이전 Issue·PR 제외·자기 자신 제외 |
| 임베딩 후보 검색 | `title + body` 임베딩과 Top-K 검색 |
| 중복 최종 판정 | target/candidate 쌍의 LLM 판정 |
| PR 요약 | PR 입력 구성, LLM 호출, 결과 JSON 검증 |
| `original_labels` 변환 | 현재 `labels`를 평가 데이터 필드로 분리 |
| `mapped_label` 생성 | 저장소별 라벨 대응표 적용 |
| `dataset_split` | `dev`와 `eval` 분리 및 고정 |
| `duplicate_target` | 원본 중복 링크·라벨·댓글에서 정답 생성 |
| `duplicate_evidence` | 중복 정답 근거 별도 저장 |
| 평가 지표 | Accuracy·F1·Hit@K·Recall@K 계산 |

따라서 현재 코드는 **OAuth 인증과 GitHub 원본 수집 계층**까지 구현되었으며,
기능별 변환기·Agent·평가 계층은 2차시 스키마 확정 후 구현해야 한다.

2026-09-27에 선정한 세 저장소의 일반 Issue 12건·PR 3건을 조회해 현재 정규화 함수로
필드와 라벨 규칙을 확인했다. 표본 링크, 저장 필드 대응, patch 잘림과 남은 제한은
[#8 설계 문서의 수집 필드 검증 결과](llm-candidates-and-io.md#수집-필드-검증-결과-2026-09-27)에 기록한다.

## 5. 기능별 모델 입력과 출력

### 5.1 Issue 유형 분류

모델 입력:

```json
{
  "title": "OAuth callback fails",
  "body": "state mismatch..."
}
```

출력 초안:

```json
{
  "type": "bug",
  "rationale": "기존 기능이 의도대로 동작하지 않는다."
}
```

허용 값은 `bug`, `feature-request`, `question`이다. 정보 부족 시 `null` 또는
`needs_review`를 사용하는 방식은 2차시에서 확정한다.

### 5.2 우선순위 추정

1차 모델 입력은 `title`, `body`만 사용한다.

```json
{
  "priority": "high",
  "rationale": "핵심 기능을 사용할 수 없고 대체 방법이 명시되지 않았다."
}
```

허용 값은 `high`, `medium`, `low`다. 유형과 우선순위는 독립적으로 판단한다.
추가 입력 필드는 실제 데이터에서 필요성을 확인한 뒤 결정한다.

### 5.3 중복 Issue 후보 생성과 판정

강세웅 담당 #8의 [중복 판정 예시](llm-candidates-and-io.md#중복-판정-예시)와 같은 형식을 사용한다.
아래 JSON은 형식 설명용 가상 예시이며 실제 모델 호출 결과가 아니다.

후보 필터 조건:

1. 같은 `repository`의 항목
2. 일반 Issue만 선택 (`issues[]`에 저장된 항목 또는 `is_pull_request=false`)
3. 신규 Issue보다 `created_at`이 이전인 Issue
4. 자기 자신 제외

후보 검색에서는 `title + body`를 사용하고, 후보 쌍의 판정 입력은 다음과 같다.
저장소·작성 시각·원본 URL은 호출 외부에서 관리하고, `number`는 결과 연결용으로만 사용한다.
원본 라벨과 평가 정답, 사후 중복 확인 정보는 모델 입력에서 제외한다.

```json
{
  "target_issue": {
    "number": 15,
    "title": "OAuth callback fails",
    "body": "A callback with the expected state is rejected with a state mismatch error."
  },
  "candidate_issue": {
    "number": 3,
    "title": "OAuth state validation error",
    "body": "The callback rejects the expected state as a mismatch."
  }
}
```

출력 예시:

```json
{
  "target_issue_number": 15,
  "candidate_issue_number": 3,
  "verdict": "duplicate",
  "confidence": 0.91,
  "evidence": [
    "두 이슈 모두 OAuth callback에서 state 검증이 실패하는 현상을 설명한다."
  ],
  "rationale": "재현 조건과 예상 동작이 동일하다."
}
```

`verdict`는 `duplicate`, `related`, `not_duplicate`, `insufficient_information` 중 하나다.
`confidence`는 이 예시에서 설명용 값이며, 자동 처리 임계값이나 평가 기준을 확정하지 않는다.

### 5.4 PR 요약

강세웅 담당 #8의 [PR 요약 예시](llm-candidates-and-io.md#pr-요약-예시)와 같은 형식을 사용한다.
아래 JSON은 형식 설명용 가상 예시이며 실제 모델 호출 결과가 아니다.

모델 입력에는 PR의 `title`, `body`, `files[]`와 제한된 `patch`, `patch_truncated`를 사용한다.
저장소·PR URL·브랜치 등 원본 추적 정보는 별도로 보관한다.
`commit_messages`는 필요성이 확인된 경우에만 추가한다.

```json
{
  "number": 21,
  "title": "Add OAuth callback handler",
  "body": "Adds state and PKCE validation, exchanges the authorization code for an access token, and verifies the authenticated user. Pending state is stored in memory and is lost on restart. Adds state reuse and expiration tests; test execution results are not included.",
  "files": [
    {
      "filename": "src/oauth.py",
      "patch": "+code_verifier = pending.consume(state)\n+access_token = oauth.exchange_code(code=code, code_verifier=code_verifier)\n+client = GitHubClient(access_token)\n+response = client.authenticated_user()",
      "patch_truncated": false
    },
    {
      "filename": "tests/test_oauth.py",
      "patch": "+def test_state_is_one_use():\n+    store = PendingAuthorizationStore()\n+    state, verifier, _ = store.create()\n+    assert store.consume(state) == verifier\n+    assert store.consume(state) is None\n+\n+def test_expired_state_is_rejected():\n+    now = [0.0]\n+    store = PendingAuthorizationStore(ttl_seconds=10, clock=lambda: now[0])\n+    state, _, _ = store.create()\n+    now[0] = 11.0\n+    assert store.consume(state) is None",
      "patch_truncated": false
    }
  ]
}
```

출력 예시:

```json
{
  "summary": "OAuth callback에서 code/state를 처리하고 인증된 사용자 확인을 추가한다.",
  "key_changes": [
    "state 및 PKCE 검증 추가",
    "Access Token 교환 추가",
    "OAuth 단위 테스트 추가"
  ],
  "affected_areas": [
    "authentication",
    "github-api"
  ],
  "tests": [
    "OAuth state 재사용 및 만료 테스트"
  ],
  "risks": [
    "서버 재시작 시 진행 중 OAuth 상태가 사라짐"
  ],
  "breaking_changes": [],
  "insufficient_information": false
}
```

`tests`는 입력에서 확인한 테스트 관련 내용이며 테스트 성공을 뜻하지 않는다.
`breaking_changes`의 빈 목록도 호환성 검증 완료를 뜻하지 않는다.
patch가 잘렸거나 필요한 파일이 없으면 `insufficient_information=true`로 표시하고
누락 내용을 `risks`에 기록한다. 입력에 없는 동작·실행 결과는 추측하지 않는다.

## 6. 평가용 데이터

평가 데이터는 모델 입력과 별도로 저장한다.

| 필드 | 의미 |
|---|---|
| `original_labels` | 공개 저장소의 원래 라벨 |
| `mapped_label` | 팀 공통 유형으로 변환한 정답 |
| `dataset_split` | `dev` 또는 `eval` |
| `duplicate_target` | 실제 중복 대상 Issue |
| `duplicate_evidence` | 중복 정답의 근거 |

다음 정보는 정답 확인에만 사용하고 모델 입력에서 제외한다.

- `duplicate` 원본 라벨
- `duplicate of #123` 같은 명시적 링크
- maintainer가 사후에 남긴 중복 확인 댓글
- `mapped_label`, `dataset_split`, `duplicate_target`, `duplicate_evidence`

평가 데이터 예시:

```json
{
  "issue_number": 15,
  "original_labels": ["kind/bug"],
  "mapped_label": "bug",
  "dataset_split": "eval",
  "duplicate_target": 3,
  "duplicate_evidence": "Maintainer가 #3의 중복으로 종료했다."
}
```

## 7. 라벨 정책

회의에서 사용할 데이터 저장소를 VS Code, Kubernetes, scikit-learn으로 정했다.
각 저장소의 원본 라벨은 아래 확정된 매핑표에 따라 평가용 공통 정답으로 변환한다.
매핑은 저장소별로 적용하며, 다른 저장소의 라벨 규칙을 일괄 적용하지 않는다.

| 이슈의 의미 | VS Code | Kubernetes | scikit-learn | 공통 유형 |
|---|---|---|---|---|
| 버그 신고 | `bug` | `kind/bug` | `Bug` | `bug` |
| 기능 추가·개선 요청 | `feature-request` | `kind/feature` | `New Feature`, `Enhancement` | `feature-request` |
| 사용 질문 | `*question` | `kind/support` | `Question` | `question` |

`feature-request`는 새로운 기능의 추가와 기존 기능의 개선 요청을 모두 포함한다.
따라서 scikit-learn의 `New Feature`와 `Enhancement`를 모두 `feature-request`로 매핑한다.

여러 원본 라벨이 붙은 경우에는 매핑된 공통 유형을 기준으로 충돌을 판단한다.

- 같은 공통 유형에 대응하는 라벨들이 함께 있어도 충돌로 보지 않는다.
  예를 들어 scikit-learn의 `New Feature`와 `Enhancement`가 함께 있으면 공통 유형은
  `feature-request` 하나다.
- 서로 다른 공통 유형에 대응하는 라벨들이 함께 있으면 팀 검토 대상으로 분류한다.
  예를 들어 `Bug`와 `Enhancement`가 함께 있으면 `bug`와 `feature-request`가 충돌하므로,
  검토 전에 단일 `mapped_label`을 임의로 확정하지 않는다.

원본 라벨은 `original_labels`에 보존하고, 매핑·검토를 거쳐 확정한 공통 유형은
`mapped_label`로 별도 관리한다. 두 필드는 평가용이며 모델 입력에서 제외한다.
현재 수집 코드의 원본 라벨 필드명은 `labels`이며, 위 정책은 데이터 준비 단계에
적용할 기준이다. 이 문서 수정으로 라벨 변환 기능이 구현된 것은 아니다.

라벨이 없거나 위 매핑표에 없는 라벨만 있는 항목의 검토·제외 기준은 추가 합의가
필요하다. 이런 항목을 임의로 `question` 등 특정 유형에 배정하지 않는다.

다음 라벨은 세 가지 공통 유형에 직접 매핑하지 않고 별도 용도로 보관한다.

| 원본 라벨 예시 | 용도 |
|---|---|
| `duplicate` | 중복 정답 생성에 사용 |
| `info-needed` | 필요 시 `needs_review` 검토 참고 |

위 저장소별 매핑표는 벤치마크 저장소마다 다른 라벨을 평가용 공통 유형으로 바꾸는 기준이다.
현재 개발 저장소의 업무 관리 라벨을 변경하는 표가 아니다.

개발 저장소는 기존 `type:*`, `area:*`, `status:*`, `priority:*` 라벨을 유지한다.
시연 저장소에는 필요할 경우 다음과 같이 Agent 결과 전용 라벨을 별도로 둔다.

```text
agent:type:bug
agent:type:feature-request
agent:type:question
agent:priority:high
agent:priority:medium
agent:priority:low
agent:needs-review
```

## 8. 개발·평가 데이터 분리

| 구분 | 사용 시점과 목적 |
|---|---|
| `dev` | 3~8차시 개발, 7차시 초기 성능 확인, 8차시 오류 분석·개선 |
| `eval` | 9차시 초기 버전과 개선 버전의 최종 비교 |

`eval` 정답을 확인하며 prompt나 규칙을 수정하지 않는다. 같은 `eval` 데이터에서
초기 버전과 개선 버전을 모두 실행해 비교한다.

## 9. 평가 방향

| 기능 | 평가 방향 |
|---|---|
| 유형 분류 | Accuracy, Precision, Recall, F1 |
| 우선순위 | 기준표 일치 여부와 근거의 입력 일치 여부를 수동 검토 |
| 중복 후보 추천 | Hit@K 또는 Recall@K 중심, 필요 시 Precision@K |
| 중복 이진 판정 | 최종 기능 범위를 확정한 뒤 분류 지표 결정 |
| PR 요약 | 핵심 변경 포함, 사실 오류, 중요 누락, 불필요한 추측 수동 평가 |

정량 평가의 우선순위는 유형 분류와 중복 탐지에 둔다. 우선순위와 PR 요약은
기능을 구현하되 초기 단계에서는 수동·보조 평가로 관리한다.

## 10. 2차시 확정 사항

사용 저장소·공통 라벨 매핑·라벨 충돌 처리 규칙은 회의에서 확정하여 7장에 반영했다.
아래 항목의 남은 결정을 확인한 뒤 문서 전체의 상태를 `확정`으로 변경한다.

1. 공통 수집 필드와 필드명 확정
2. 현재 수집 코드와 목표 스키마의 변환 위치 결정
3. 기능별 실제 모델 입력 필드 확정
4. 평가용 필드와 모델 입력의 물리적 분리 방식 결정
5. 저장소별 원본 라벨 매핑표·충돌 규칙은 확정; 라벨 없음·미매핑 항목의 처리 기준은 추가 합의
6. `dev`와 `eval` 분리 방법과 고정 시점 결정
7. 중복 탐지의 범위를 후보 추천 또는 이진 판정으로 확정
8. 정보 부족 시 `null` 또는 `needs_review` 표현 확정
9. 기능별 최종 평가 지표 확정

## 11. 구현 연결 지점

현재 코드에서는 다음 경계를 사용한다.

```text
OAuthSettings / GitHubOAuthClient
        ↓ Access Token
GitHubClient
        ↓ GitHub REST API 응답
collect_repository()
        ↓ metadata + issues + pull_requests
기능별 변환기 (2차시 확정)
        ↓ 모델 입력
Agent
        ↓ 모델 결과
평가 코드
        ↓ 정답과 비교
```

관련 코드:

- `src/triage_agent/config.py`: OAuth와 저장소 설정
- `src/triage_agent/github.py`: OAuth 및 GitHub REST API Client
- `src/triage_agent/collector.py`: Issue·PR 수집과 정규화
- `src/triage_agent/oauth_server.py`: OAuth callback과 최초 수집 연결
- `src/triage_agent/collect_saved.py`: 저장된 Token으로 재수집

현재 수집 JSON에는 Token과 Client Secret을 넣지 않는다. macOS/Linux에서는
민감한 로컬 파일 권한을 `0600`으로 제한하고, Windows에서는 저장 폴더의
NTFS ACL을 따른다.
