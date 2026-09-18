# LLM 후보·중복 Issue 탐지·PR 요약 초안

관련 Issue: #8  
조사 기준일: 2026-09-18

## 결정 요약

초기 실험은 무료 사용이 가능한 Gemini Flash와 Groq의 공개 모델을 우선 비교한다. 유료 API는 동일 평가 데이터에서 품질 기준선이 필요할 때만 추가한다. 최종 모델은 공급자 설명만으로 정하지 않고, 동일한 Issue 쌍과 PR 표본에서 JSON 준수율·정확성·지연시간을 측정해 선택한다.

## 후보 비교

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

평가 정답으로 사용하는 원본 `labels`와 명시적 duplicate 연결 정보는 모델 입력에서 제외한다. 검색 단계에서는 `title + body` 임베딩으로 후보를 좁히고, LLM은 target과 candidate 한 쌍을 판정한다.

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

큰 diff는 파일별 크기 제한과 전체 token budget을 적용한다. binary, lock file, 생성 파일은 기본적으로 제외하고 제외 사실을 입력 메타데이터에 남긴다.

## 출력 명세

### 중복 Issue 탐지

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

`verdict`는 `duplicate`, `related`, `not_duplicate`, `insufficient_information` 중 하나다. `confidence`만으로 자동 duplicate 처리하지 않고 근거와 임계값을 평가 데이터에서 정한다. Issue 본문의 지시문은 데이터로 취급하고 실행하지 않는다.

### PR 요약

```json
{
  "summary": "OAuth callback에서 code/state를 처리하고 인증된 사용자 확인을 추가한다.",
  "key_changes": [
    "state 및 PKCE 검증 추가",
    "Access Token 교환 추가",
    "OAuth 단위 테스트 추가"
  ],
  "affected_areas": ["authentication", "github-api"],
  "tests": ["OAuth state 재사용 및 만료 테스트"],
  "risks": ["서버 재시작 시 진행 중 OAuth 상태가 사라짐"],
  "breaking_changes": [],
  "insufficient_information": false
}
```

입력에 없는 테스트 성공 여부나 동작을 추측하지 않는다. patch가 잘렸거나 필요한 파일이 없으면 `insufficient_information`을 `true`로 설정하고 누락 정보를 `risks`에 기록한다.

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
- 원본 라벨은 평가 정답용으로 보존하되 모델 입력에서는 제거한다.
- PR patch가 없는 경우 changed files endpoint 호출 또는 누락 표시 중 하나를 명시한다.

## 참고 자료

- [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing)
- [Gemini API rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)
- [Gemini structured output](https://ai.google.dev/gemini-api/docs/structured-output)
- [Gemini API keys](https://ai.google.dev/gemini-api/docs/api-key)
- [Groq models](https://console.groq.com/docs/models)
- [Groq rate limits](https://console.groq.com/docs/rate-limits)
- [Groq structured outputs](https://console.groq.com/docs/structured-outputs)
