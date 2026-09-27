# Gemini 초기 호출·JSON 응답 연결 (#14)

담당: 강세웅. 확인일: 2026-09-27.

이 문서는 #8의 후보 조사를 이어받아 #13 분류 담당자가 사용할 **호출 계층**과
연결 방법을 정리한다. 분류 기준·최종 프롬프트·dev 전체 실행 흐름은 #13에서 관리한다.
이 코드만으로 #14의 공동 연결·동료 검토까지 완료되는 것은 아니다.

## 모델 선택 및 현재 제약

초기 모델은 `gemini-3.8-flash`다. 기존 Google 계정을 사용할 수 있고 무료 구간과
구조화 출력이 있어 작은 분류 연동 실험을 시작하기에 적합하다고 판단했다.
여러 모델의 분류 정확도를 비교해 최종 모델을 선정한 결과는 아니다.

- 공식 모델 한도: 입력 1,048,576토큰, 출력 65,536토큰. 구조화 JSON 지원.
  [모델 문서](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash)
- Free tier 입력·출력 무료. 무료 데이터는 서비스 개선에 사용될 수 있으므로
  연결 예제는 가상 텍스트를 사용한다. [가격표](https://ai.google.dev/gemini-api/docs/pricing)
- 실제 RPM/TPM/RPD는 프로젝트별로 다르며 AI Studio에서 확인해야 한다.
  계정별 수치는 아직 기록하지 않았다. [호출 한도](https://ai.google.dev/gemini-api/docs/rate-limits)
- `v1beta/interactions` REST API를 사용한다. `store=false`, 도구·검색·스트리밍 없이
  한 번의 텍스트 응답을 받는다. [API 명세](https://ai.google.dev/api/interactions-api)

## 설치 및 연결 확인

Python 3.11 이상과 표준 라이브러리만 사용하므로 추가 패키지 설치는 필요 없다.
저장소 루트의 기존 `.env`에 다음 항목을 추가한다. 기존 OAuth 설정은 보존한다.

```dotenv
GEMINI_API_KEY=발급받은_키
GEMINI_MODEL=gemini-3.8-flash
```

`.env`는 실행 예제가 직접 읽으므로 편집기의 터미널 환경 주입 설정은 필요 없다.
이미 터미널에 같은 환경변수가 있으면 터미널 값이 우선한다.

macOS/Linux, 저장소 루트에서:

```bash
PYTHONPATH=src python3 -m triage_agent.gemini_example --demo
```

Windows PowerShell:

```powershell
$env:PYTHONPATH = "src"
python -m triage_agent.gemini_example --demo
```

`--demo`는 저장 시 앱이 종료된다는 가상 이슈 1건을 실제 API에 보낸다.
이 예제의 짧은 지시문은 연결 확인용이며 팀의 분류 프롬프트 v0가 아니다.
무료 한도 내에서 시작하되 실제 계정의 요금제·한도는 실행자가 확인한다.

반환 종료 코드는 성공 0, 호출·응답 실패 1, 설정·입력·저장 오류 2다.
실행 결과 파일은 Git에서 제외된 `data/llm/`에 저장된다. 파일 권한은
macOS/Linux에서 소유자 읽기·쓰기(0600)이며 Windows는 폴더 ACL을 따른다.

## #13과 연결할 함수

```python
import os
from triage_agent.config import load_env_file
from triage_agent.gemini import GeminiClient, GeminiSettings

load_env_file()
client = GeminiClient(os.environ["GEMINI_API_KEY"], GeminiSettings())
result = client.generate(
    title=issue["title"],
    body=issue["body"],
    instruction=classification_prompt,  # #13에서 제공
    prompt_version="v0",               # 실제 프롬프트 버전
)
if result["status"] == "success":
    prediction = result["prediction"]
else:
    prediction = None  # 호출 실패를 임의의 유형이나 정보 부족으로 바꾸지 않는다.
```

`repository`, `number` 등 원본 연결 정보와 평가 정답은 호출자 쪽에서 보관한다.
호출 함수에는 제목·본문과 별도 프롬프트만 전달한다. 로그의 입력 해시와 프롬프트 해시는
실행 조건 대조용이며, 원본 데이터·프롬프트 파일 보관 자체를 대신하지 않는다.

파일로 실행하려면 `title`, `body` 두 필드만 있는 JSON과 #13의 프롬프트 파일을 준비한다.
다른 필드가 섞여 있으면 CLI가 거부한다.

```bash
PYTHONPATH=src python3 -m triage_agent.gemini_example \
  --input issue-input.json --prompt classification-prompt.txt --prompt-version v0
```

정상 응답의 **형식 설명용 예시**:

```json
{"type": "bug", "rationale": "저장 동작으로 앱이 종료되는 오류를 보고한다."}
```

기본 계약은 `type`이 `bug`, `feature-request`, `question` 중 하나이며
`rationale`은 비어 있지 않은 문자열이다. 추가 필드, 누락, 잘못된 유형은 거부한다.
JSON 중복 키·NaN·잘린 응답도 성공으로 처리하지 않는다.

보류 표현은 아직 팀 합의 전이다. `allow_review=True` 또는 `--allow-review`로
아래 **제안 계약**을 선택적으로 시험할 수 있다. 기본 실행에서는 사용하지 않는다.

```json
{"type": null, "rationale": "판단에 필요한 정보가 부족하다.", "needs_review": true}
```

이 모드에서는 정상 유형에도 `needs_review=false`가 필수다. `needs_review=true`이면
`type=null`이어야 한다. 합의 후 #13 프롬프트도 같은 계약을 사용해야 한다.
API 실패는 이 보류 응답으로 변환하지 않으며 항상 `prediction=null`을 반환한다.

## 오류·길이 제한·재시도

| 결과 status | 의미 |
|---|---|
| `success` | 호출 완료 및 응답 계약 검증 통과. 분류 정답 여부까지 보장하지 않음 |
| `server_error` | HTTP 5xx. 503 혼잡도 여기에 포함 |
| `rate_limited` | HTTP 429 사용 한도 초과 |
| `auth_error` | HTTP 401/403. 키·권한·프로젝트 접근 확인 필요 |
| `api_error` | 그 외 HTTP 오류. 요청 설정·모델 지원 여부 등 확인 필요 |
| `timeout` / `network_error` | 응답 대기 시간 초과 / 연결 실패 |
| `invalid_json` / `invalid_schema` | JSON 파싱 실패 / 분류 응답 계약 불일치 |
| `invalid_response` / `incomplete_response` | 필요한 응답 구조 누락 / 모델 작업 미완료 |
| `input_too_long` | 로컬 입력 제한 초과. API 호출하지 않음 |

아래는 구현 검증을 위한 **초기값**이며 #13 담당자와 확정해야 한다.

- `max_input_chars=12000`: 제목+본문+프롬프트의 문자 수 제한이다. 토큰 수나
  모델의 최대 문맥 한도가 아니다. JSON 포장·스키마는 문자 합계에 포함되지 않는다.
  기본은 초과 시 거부한다. `--truncate-body`를 명시하면 본문 끝만 자르고
  원래/전송 본문 길이와 `input.truncated=true`를 기록한다. 제목·프롬프트만으로
  한도를 넘으면 자르지 않고 거부한다.
- `max_output_tokens=1024`, `thinking_level=low`. 출력 한도 때문에 미완료 응답이
  반환되면 정상 분류로 취급하지 않는다.
- 네트워크 timeout 기본 45초. 이는 소켓 대기 제한이며 전체 작업의 엄격한 마감 시간이 아니다.
- **기본 재시도 0회.** `--max-retries 1` 또는 `2`를 명시한 경우에만 429/5xx/연결 오류를
  재시도한다. 대기 시간은 2초, 4초이며 HTTP `Retry-After`가 더 길면 따른다.
  30초보다 긴 `Retry-After`는 줄여서 재시도하지 않고 결과를 반환한다.
  인증·JSON·계약 오류는 자동 재시도하지 않는다. timeout 이후 서버가 이미 처리했을
  가능성이 있어 재시도는 추가 사용량을 만들 수 있다.

기록에는 모델·프롬프트 버전/해시·입력 해시·길이/잘림·설정·시도별 HTTP 상태·
확인 가능한 사용량·총 지연시간·최종 예측이 포함된다. API가 사용량을 주지 않으면
사용량을 0으로 추측하지 않는다. 입력 본문, API 키, HTTP 헤더, 오류 원문,
모델의 내부 사고 내용은 저장하지 않는다.

## 검증 및 남은 작업

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

모의 네트워크를 사용하는 테스트로 정상 JSON, 보류 계약 opt-in, 잘못된 JSON/필드,
503·429·시간 초과 구분, 재시도 상한, 긴 본문 처리, CLI 입력 제한과 키 비노출을 확인한다.
이 테스트는 실제 LLM 호출 성공의 증거가 아니다.

- 실제 호출 결과는 아래 실행 확인 기록에 별도로 남긴다.
- 재시도·입력 제한·보류 계약의 팀 확정이 필요하다.
- #13 실행 흐름에서 대표 사례 공동 연결과 #15 기록 항목 검토가 남아 있다.
- 동료 리뷰·PR 병합과 #14 완료 처리는 아직 진행하지 않았다.

## 실행 확인 기록

2026-09-27: 코드 추가 전 연결 확인은 HTTP 503(혼잡)으로 실패했다.

2026-09-27 12:45 KST: `--demo` 실제 호출이 1회 만에 HTTP 200으로 성공했다.

- 모델: `gemini-3.8-flash`, 프롬프트: `connection-demo-v1`.
- 응답: `type=bug`, JSON/필수 필드 검증 통과.
- 지연시간: 16.379초. 입력 67·출력 46·생각 0·총 113토큰(API 반환값).
- 입력 잘림 없음, 자동 재시도 없음.
- 로컬 기록: `data/llm/gemini-20260927T034525Z-94f2b6a3.json` (Git 제외).
- 실제 `rationale`: "The application closes unexpectedly (crashes) when attempting a normal action (saving), which represents unintended behavior or a defect in the software."

이는 가상 사례 1건의 호출/응답 연결 확인이며 분류 성능 평가나 #13 공동 실행 결과는 아니다.
계정의 실제 무료 한도·보류 사례 실제 호출·공동 연결 확인은 아직 남아 있다.
이 최초 로그의 `input.sent`는 이후 코드에서 연결 시도와 서버 수신을 혼동하지 않도록
`input.request_attempted`로 이름을 명확히 했다.
