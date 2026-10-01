# LLM 후보·중복 Issue 탐지·PR 요약 초안

관련 Issue: #8  
LLM 후보 조사 기준일: 2026-09-18

수집 필드·라벨 표본 검증일: 2026-09-27

## 결정 요약

분석에 사용할 GitHub 저장소는 회의에서 VS Code, Kubernetes, scikit-learn으로 확정했다.
저장소별 원본 라벨을 `bug`, `feature-request`, `question`으로 매핑하는 기준과
여러 라벨이 함께 붙었을 때의 충돌 처리는
[통합 데이터 스키마의 라벨 정책](data-schema.md#7-라벨-정책)을 따른다.

초기 실험은 무료 사용이 가능한 Gemini Flash와 Groq의 공개 모델을 우선 비교한다. 유료 API는 동일 평가 데이터에서 품질 기준선이 필요할 때만 추가한다. 최종 모델은 공급자 설명만으로 정하지 않고, 동일한 Issue 쌍과 PR 표본에서 JSON 준수율·정확성·지연시간을 측정해 선택한다.

## LLM 후보 비교

| 후보 | 무료 조건 | 입력 길이·호출 제한 | 구조화 출력 | 초기 판단 |
|---|---|---|---|---|
| Google `gemini-3.8-flash` | Free tier에서 입력·출력 토큰 무료. 무료 입력은 제품 개선에 사용될 수 있으므로 공개 GitHub 데이터만 전송 | 모델별 RPM·TPM·RPD가 다르고 실제 적용치는 AI Studio에서 확인. 긴 context와 JSON Schema 지원 | 지원 | 긴 PR 설명·diff 요약과 한국어 설명 비교의 1순위 후보 |
| Groq `openai/gpt-oss-20b` | Free tier 제공. Developer tier는 결제수단 등록 후 종량제 | 공식 Rate Limit 표 기준 Free tier 30 RPM, 1K RPD, 8K TPM, 200K TPD. Context 131,072 tokens | `strict: true` JSON Schema 지원 | 빠른 반복과 안정적인 JSON 출력 후보 |
| Groq `qwen/qwen3.8-27b` | Free tier 제공. 계정에 표시되는 실제 한도 확인 필요 | 공식 Rate Limit 표 기준 Free tier 30 RPM, 1K RPD, 8K TPM, 200K TPD. Context 131,072 tokens | `strict: true` JSON Schema 지원 | 한국어와 분류 근거 품질을 GPT-OSS와 비교할 후보 |

호출 제한과 제공 모델은 변경될 수 있다. 실험 결과에는 모델 ID, 조사·실행일, 콘솔에 표시된 실제 한도를 함께 기록한다. Gemini의 2026년 신규 키는 auth key가 기본이며, 기존 unrestricted standard key는 사용할 수 없으므로 키 유형도 실행 환경에 기록한다.

## 공통 입력 필드

### 중복 Issue 탐지

필수:

- `repository`, `number`, `title`, `body`, `html_url`
- `state`, `created_at`, `updated_at`

보조:

- `comments` 또는 maintainer의 중복 언급
- `linked_issues`
- `author_association`

평가 정답으로 사용하는 원본 `labels`(`original_labels`), 매핑된 공통 정답 `mapped_label`, 명시적 duplicate 연결 정보는 모델 입력에서 제외한다. 검색 단계에서는 `title + body` 임베딩으로 후보를 좁히고, LLM은 target과 candidate 한 쌍을 판정한다.

### PR 요약

필수:

- `repository`, `number`, `title`, `body`, `html_url`
- `base.ref`, `head.ref`, `state`, `merged_at`
- `changed_files`, `additions`, `deletions`
- 변경 파일 경로와 제한된 patch

보조:

- commit 메시지
- 테스트 관련 파일과 CI 결과
- 연결된 Issue

현재 수집기는 patch를 파일당 최대 12,000자로 저장하고 `patch_truncated`로 잘림을 표시한다.
모델 입력을 구성하는 후속 단계에서는 전체 token budget을 적용하고, binary, lock file,
생성 파일을 기본적으로 제외하며 제외 사실을 입력 메타데이터에 남기도록 설계한다.
전체 입력 길이 제한과 파일 제외 처리는 아직 구현되지 않았다.

## 수집 필드 검증 결과 (2026-09-27)

### 검증 범위와 방법

기존 `data/oauth/`의 2026-09-18 수집 JSON 4개는 모두 개발 저장소
`Gosu0101/llm-github-triage-agent`의 실습 결과였다. 가장 최근 파일에는 일반 Issue 8건,
PR 2건과 변경 파일 14개가 있었고, 필수 필드와 patch가 저장되어 있었다.
이 자료만으로는 선정한 세 저장소의 라벨 매핑을 검증할 수 없어 실제 표본을 추가 조회했다.

GitHub 연결 도구로 세 저장소의 공개 API를 읽고, 조회한 표본에 현재 수집 코드의
`_normalize_issue()`와 `_normalize_pull_request()`를 적용해 저장 형식을 확인했다.
이번 검증은 OAuth 재인증이나 `collect_repository()` 전체 실행을 검증한 결과가 아니다.

각 매핑 라벨이 붙은 일반 Issue를 골랐으며, PR 목록에서는 저장소별 1건을 골라
상세 정보와 변경 파일을 추가 조회했다. 라벨 충돌 규칙을 확인할 scikit-learn Issue
2건도 포함했다. 이는 필드와 규칙을 확인하기 위한 표본이며 성능 평가용 무작위 표본이 아니다.
이번에 확인한 항목은 개발 검토용으로 기록하고 향후 최종 eval에서 제외한다.

| 저장소 | 일반 Issue | PR | PR 변경 파일 | 저장 후 patch 잘림 |
|---|---:|---:|---:|---:|
| `microsoft/vscode` | 3건 | 1건 | 3개 | 0개 |
| `kubernetes/kubernetes` | 3건 | 1건 | 11개 | 0개 |
| `scikit-learn/scikit-learn` | 6건 | 1건 | 8개 | 1개 |
| 합계 | 12건 | 3건 | 22개 | 1개 |

확인한 Issue 12건에는 제목·본문·번호·상태·작성/수정 시각·URL·원본 라벨이 모두 있었다.
저장소 이름은 `metadata.repository`로 연결한다. PR 3건에서도 제목·본문·브랜치·변경 통계와
파일 목록을 확인했고, 조회한 파일 수는 각 PR의 `changed_files`와 일치했다.
이번 PR 표본은 모두 미병합 상태이므로 `merged_at=null`은 정상 값이다.

### 실제 라벨과 회의 매핑 기준 대조

아래는 조회 시점의 원본 라벨 중 매핑에 관련된 라벨을 발췌한 것이다.
다른 관리용 라벨이 함께 있어도 서로 다른 공통 유형으로 매핑되지 않으면 유형 충돌로 보지 않는다.

| 저장소 | 원본 라벨 | 실제 Issue | 규칙 적용 결과 |
|---|---|---|---|
| VS Code | `bug` | [#323414](https://github.com/microsoft/vscode/issues/323414) | `bug` |
| VS Code | `feature-request` | [#322876](https://github.com/microsoft/vscode/issues/322876) | `feature-request` |
| VS Code | `*question` | [#329965](https://github.com/microsoft/vscode/issues/329965) | `question` |
| Kubernetes | `kind/bug` | [#142361](https://github.com/kubernetes/kubernetes/issues/142361) | `bug` |
| Kubernetes | `kind/feature` | [#142430](https://github.com/kubernetes/kubernetes/issues/142430) | `feature-request` |
| Kubernetes | `kind/support` | [#133512](https://github.com/kubernetes/kubernetes/issues/133512) | `question` |
| scikit-learn | `Bug` | [#35029](https://github.com/scikit-learn/scikit-learn/issues/35029) | `bug` |
| scikit-learn | `New Feature` | [#30223](https://github.com/scikit-learn/scikit-learn/issues/30223) | `feature-request` |
| scikit-learn | `Enhancement` | [#31315](https://github.com/scikit-learn/scikit-learn/issues/31315) | `feature-request` |
| scikit-learn | `Question` | [#21846](https://github.com/scikit-learn/scikit-learn/issues/21846) | `question` |
| scikit-learn | `New Feature` + `Enhancement` | [#14257](https://github.com/scikit-learn/scikit-learn/issues/14257) | `feature-request` 하나, 충돌 아님 |
| scikit-learn | `Bug` + `Enhancement` | [#24411](https://github.com/scikit-learn/scikit-learn/issues/24411) | `bug`와 `feature-request` 충돌, 팀 검토 필요 |

12건 중 11건은 단일 공통 유형으로 매핑되고 1건은 팀 검토 대상으로 분류되었다.
이는 회의 매핑 규칙의 적용 결과이며, 모델의 예측이나 내용 검토를 마친 최종 정답을 의미하지 않는다.
원본 라벨과 검토 결과는 모델 입력과 분리한다.

Kubernetes의 라벨별 Issues API 응답에는 PR도 섞여 있었다. `pull_request`가 있는 항목을
제외한 일반 Issue만 위 표에 포함했다. `kind/feature`는 첫 조회 3건이 모두 PR이어서
`is:issue` 조건으로 다시 조회한 일반 Issue를 사용했다. 실제 수집에서도 PR 제외와 충분한
페이지 조회를 함께 확인해야 한다.

### 문서와 현재 저장 형식의 대응

| 문서·목표 스키마 | 현재 저장 위치·이름 | 연결 방법 또는 남은 작업 |
|---|---|---|
| `repository` | `metadata.repository` | 개별 Issue·PR을 처리할 때 같은 저장소 정보 연결 |
| `original_labels` | `issues[].labels` | 평가 데이터 준비 단계에서 이름을 맞추고 원본 값 보존 |
| `mapped_label` | 수집 JSON에는 없음 | 별도 평가 데이터 준비 단계에서 생성·검토 |
| `base.ref`, `head.ref` | `base_ref`, `head_ref` | 문서의 GitHub API 표기와 저장 필드의 대응이며 재수집 불필요 |
| `changed_files_count` | 정수형 `changed_files` | 목표 스키마로 변환할 때 개수 필드임을 명시; 파일 목록은 `files[]` |
| `is_pull_request` | `issues[]`와 `pull_requests[]`로 분리 | 배열을 합칠 경우 명시적 구분 필드 추가 |

이번 확인용 파일에서 매핑 규칙을 적용했지만, 프로그램의 자동 라벨 변환·모델 입력 생성
기능을 구현한 것은 아니다. 원본 수집 자료를 통째로 모델 입력에 전달하지 않는다.

### PR 변경 정보와 남은 제한

확인한 PR은 [VS Code #338165](https://github.com/microsoft/vscode/pull/338165),
[Kubernetes #142446](https://github.com/kubernetes/kubernetes/pull/142446),
[scikit-learn #35036](https://github.com/scikit-learn/scikit-learn/pull/35036)이다.
22개 변경 파일 모두 API 응답에 patch가 있었지만, scikit-learn PR의
`sklearn/linear_model/tests/test_logistic.py`는 현재 수집기의 제한에 따라 12,000자로
잘렸고 `patch_truncated=true`가 저장되는 것을 확인했다.

- 요약 단계에서는 이 잘림 정보를 전달하고, 정보 부족 여부와 누락된 범위를 결과에 표시해야 한다.
- 댓글 본문·연결 Issue·`author_association`·커밋 메시지·CI 결과는 현재 정규화된 수집 결과에 없다.
  일부는 API 원본에 있어도 저장 과정에서 보존하지 않으며, 후속 보조 항목으로 관리한다.
- PR 변경 파일 조회는 현재 첫 페이지 최대 100개까지다. 이번 표본에서는 파일 수가 일치했지만,
  대형 PR 전체 수집을 검증한 결과는 아니며 추가 페이지 처리 또는 누락 표시가 필요하다.
- 전체 모델 입력 길이 제한, binary·lock·생성 파일 제외, 모델 입력·평가 정답 분리는 후속 구현 대상이다.
- 라벨 없음·미매핑 항목의 처리 기준과 충돌 사례의 최종 정답은 추가 합의·검토가 필요하다.

조회 원본, 저장 형식으로 정규화한 저장소별 JSON, `verification-summary.json`은 로컬
`data/oauth/issue8-field-audit-20260927/`에 보관한다. 이 경로는 Git에서 제외되므로 PR에는
원문 데이터 대신 위 표본 링크·검증 방법·집계 결과를 남긴다.

## 입력·출력 예시

이 절은 #8의 강세웅 담당 범위인 중복 탐지·PR 요약의 인터페이스를 정리한다.
아래 JSON은 현재 수집 필드 구조에 맞춘 **형식 설명용 가상 예시**이며 실제 모델 호출 결과가 아니다.
실제 표본으로 만든 입력 예시는 아래 별도 항목에 기록한다.

### 중복 판정 예시

같은 저장소의 일반 Issue 중 자기 자신을 제외하고, 대상보다 먼저 작성된 후보를 선택한 뒤
아래 형태로 전달한다. `repository`, 작성 시각, 원본 URL은 후보 선택·원본 추적용으로
호출 외부에 보관한다. `number`는 결과 연결용 식별자이며 판단 근거로 사용하지 않는다.
모델은 제목과 본문을 비교하며, 본문에 포함된 지시문도 데이터로만 취급한다.

입력 예시:

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
`confidence`의 예시 값은 측정 결과나 보정된 확률이 아니다. 이 값만으로 자동 중복 처리를
결정하지 않으며, 평가 기준과 임계값은 별도 검토 대상이다.
원본 라벨·매핑된 정답·사후 중복 확인 댓글·정답으로 사용하는 연결 정보는 입력에 넣지 않는다.

### PR 요약 예시

제목·본문과 변경 파일의 patch를 전달하고, 파일별 `patch_truncated`를 함께 넘긴다.
저장소 이름·PR URL·브랜치·수집 시각은 원본 추적 정보로 별도 보관한다.

입력 예시:

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

`tests`는 입력에서 확인한 테스트 관련 내용을 기록한다. 테스트 코드가 있다는 이유만으로
실행 성공을 주장하지 않는다. `breaking_changes`의 빈 목록은 입력에서 확인된 항목이
없다는 뜻이며, 호환성 검증 완료를 의미하지 않는다.
patch가 잘렸거나 필요한 파일이 없으면 `insufficient_information=true`로 표시하고,
어떤 정보가 부족한지 `risks`에 기록한다. 입력에 없는 동작이나 결과는 추측하지 않는다.

### 실제 표본으로 만든 입력 예시

앞서 확인한 VS Code 표본에서 필요한 필드만 골라 로컬
`data/oauth/issue8-field-audit-20260927/input-examples.json`에 입력 예시를 저장했다.
원본 추적 정보는 `source`에, 모델에 전달할 내용은 `model_input`에 분리했다.
원본 라벨·평가 정답 필드는 두 `model_input`에 포함하지 않았다.

- 중복 판정 입력: [#323414](https://github.com/microsoft/vscode/issues/323414)를 대상으로,
  같은 저장소에서 먼저 작성된 [#322876](https://github.com/microsoft/vscode/issues/322876)를
  후보로 사용했다. 이는 입력 구조 확인용 쌍이며, 유사도 검색으로 선정했거나 중복 관계를 확인한 쌍이 아니다.
- PR 요약 입력: [#338165](https://github.com/microsoft/vscode/pull/338165)의 제목·본문과
  변경 파일 3개의 patch·잘림 표시를 사용했다.
- 실제 표본의 모델 출력은 생성하지 않았다. 위 가상 출력과 실제 표본의 결과를 혼동하지 않는다.
  이번 작업에는 수집기·분류기·평가 데이터 생성 기능 구현이나 모델 호출을 포함하지 않는다.

## 비교 실험

1. 확정된 평가 데이터와 분리된 개발 표본을 준비한다.
2. 후보마다 동일 prompt, JSON Schema, temperature, token budget을 사용한다.
3. 중복 탐지는 `verdict` 정확도/F1, JSON 준수율, 근거의 입력 일치 여부를 기록한다.
4. PR 요약은 핵심 변경 포함, 사실 오류, 중요 누락, JSON 준수율을 사람이 검토한다.
5. 응답 지연시간과 실패·재시도 횟수도 기록한다.
6. 품질이 비슷하면 무료 한도, 재현성, 데이터 정책이 적합한 후보를 선택한다.

## #4 수집 담당자에게 전달할 사항

- 위 입력 필드의 실제 수집 가능 여부와 REST endpoint를 확인한다.
- Issue endpoint에 섞여 반환되는 PR은 `pull_request` 키로 분리한다.
- 원본 응답과 모델 입력용 정제 데이터를 별도 보관한다.
- 원본 라벨과 매핑된 공통 정답은 평가용으로 보존하되 모델 입력에서는 제거한다. 매핑·충돌 처리는 [공통 라벨 정책](data-schema.md#7-라벨-정책)을 따른다.
- PR patch가 없는 경우 changed files endpoint 호출 또는 누락 표시 중 하나를 명시한다.

## 참고 자료

- [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing)
- [Gemini API rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)
- [Gemini structured output](https://ai.google.dev/gemini-api/docs/structured-output)
- [Gemini API keys](https://ai.google.dev/gemini-api/docs/api-key)
- [Groq models](https://console.groq.com/docs/models)
- [Groq rate limits](https://console.groq.com/docs/rate-limits)
- [Groq structured outputs](https://console.groq.com/docs/structured-outputs)
