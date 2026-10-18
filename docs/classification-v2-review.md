# 고정 v2 후속 분류 검토

형언이의 v1 검토에 따른 최소 3문장 변경을 별도 v2 프롬프트로 보존합니다.
v1 실행기·프롬프트·입력·정답·과거 결과는 바꾸지 않습니다. v2 이후에는
이 dev 15건에 맞춰 추가 튜닝하지 않고 별도 9차시 eval에서 판단합니다.

## 묶음과 오프라인 검증

보류·실패·재시도·입력 길이·생성 한도의 권장안은
[실행 정책 합의 초안](classification-execution-policy-draft.md)에서 검토합니다.
이 문서는 팀 미확정 제안이며 실행기 동작·기본값을 변경하지 않습니다.

프로젝트 루트에서 `python -m pip install -e .`로 설치한 뒤 실행합니다.
공유 v2 묶음의 파일을 `data/classification-review-v2/`에 준비합니다.
다른 위치는 `--bundle`로 지정합니다. 데이터는 Git에서 제외되며 clone만으로
전달되지 않습니다. v1의 8파일과 추가 5파일, 총 13파일입니다.

- 기존: preflight.json, classification-v1-review.txt, inputs/dev-inputs.jsonl,
  inputs/reserve-inputs.jsonl, reference/dev-gold.jsonl, reference/v0-results.jsonl,
  reference/run-manifest.v0.json, reference/classification-v0-review.txt.
- 추가: v2-preflight.json, classification-v2-review.txt,
  reference/v1-gemini-results.jsonl, reference/v1-local-results.jsonl, local-settings.json.

```powershell
python -B -m triage_agent.review_v2
python -B -m triage_agent.review_v2 --provider local
```

기본은 인증정보 없이 네트워크 요청 0회입니다. 원본 v1 묶음, v2 프롬프트,
같은 모델의 v1 결과 및 환경 기록의 해시를 검사합니다. v1 결과의 입력·프롬프트
해시와 항목 구성도 대조합니다. 원본·정답을 바꾸거나 검사를 우회하지 않습니다.

## 실제 실행

```powershell
python -B -m triage_agent.review_v2 --execute --env-file .env
python -B -m triage_agent.review_v2 --provider local --execute --smoke
python -B -m triage_agent.review_v2 --provider local --execute
```

Gemini는 18건 정규 실행 후 #331658·#338087을 각 3회 **별도 추가 실행**합니다.
총 24회이며 재시도는 별도로 기록됩니다. 기존 Gemini 설정을 유지하고
시도 시작 간격은 기본 13초입니다. `--boundary-repeats 0`으로 정규 실행만
선택할 수 있지만 전체 요청 계획은 별도 3회 반복을 포함합니다.

로컬은 v2 18건만 실행하며 v0를 재실행하지 않습니다. smoke는 첫 dev 1건입니다.
모델 ID·컨텍스트·양자화·GPU offload·reasoning 설정을 실행 전에 확인합니다.
local-settings.json의 제공 값은 과거 사용자 보고이며 현재 서버 확인의 증거가
아닙니다. 실제 설정을 기록해 파일을 갱신할 경우 v2-preflight.json의 해당
파일 해시도 함께 갱신하고, 기존 실행용 묶음은 보존합니다.
컨텍스트 8192는 LM Studio 모델 로딩 설정이고 생성 한도 2048과 다릅니다.

## 결과와 판단

결과는 `data/llm/review/v2-<provider>-<UTC>-<ID>/`에 저장합니다.
`--output`으로 새 위치를 지정할 수 있고 기존 폴더는 거부합니다.

- results/: 정규 18건의 결과. dev와 예비는 구분됩니다.
- stability/: 반복 결과. 정규 예측을 대체하거나 dev 점수에 합치지 않습니다.
- summary.json, v1-v2-comparison.csv, run-manifest.json.
- classification-v1-review.txt, classification-v2-review.txt, local-settings.json.

동일 모델의 v1과 비교해 기존 정답 14건 유지 및 남은 오분류 개선을 확인합니다.
보류·실패·미실행은 dev 분모에서 제외하지 않습니다. 예비 3건은 공식 점수에
포함하지 않으며 question 일치 수치를 정답처럼 보고하지 않습니다.
#333444·#327177의 보류와 #334239의 모호성을 따로 검토합니다.
호출 단계에서 실패 결과를 받으면 기록과 요약을 저장하고 중단하며 종료 코드는 2입니다.
호출 전 `load()`의 길이·묶음 검증 거부는 stderr와 종료 코드 2만 반환하고
결과 폴더·summary를 만들지 않습니다. 모델 요청은 0회이며,
[호출 전 거부 기록 절차](classification-execution-policy-draft.md#호출-전-검증-거부의-기록--2026-10-18-보완)에 따라
별도 검증 폴더에 대상·사유·요청 0회를 남깁니다. 항목 ID는 CLI 오류에 포함되지
않으므로 원문을 바꾸지 않는 길이 대조로 확인하고, 식별 불가 시 그 사유를 기록합니다.
부분 실행은 완료된 성능 비교로 해석하지 않습니다. 결과 ZIP에는 v1·v2 원문과
환경 기록을 함께 포함해 프롬프트·설정 확인이 가능하도록 합니다.

## 기존 실행 기록과 검토 상태 — 2026-10-16

이 절은 이미 수행한 소규모 dev 실험의 인계 요약이며 최종 eval 결과가 아닙니다.
결과 ZIP과 실행용 묶음은 팀 공유 자료로 별도 전달하며 Git에 포함하지 않습니다.
작성자는 결과 전달을 완료했다고 확인했고, 형언이의 v2 검토 회신을 기다리고 있습니다.

| 실행 조건 | 기록된 결과 |
|---|---|
| Gemini v2 정규 dev | 14/15 정답. `#338087`은 feature-request로 오분류 |
| Gemini 별도 반복 | `#331658` question 3/3, `#338087` bug 1/3·feature-request 2/3 |
| 로컬 v2, max_tokens=2048 | dev 12정답·3응답 잘림. 예비 포함 18개 최초 응답 확보 |
| 로컬 실패 3건, max_tokens=4096 별도 확인 | 3건 응답 완료·2정답·1오분류(`#331658`: bug, 참조 question) |

로컬 2048은 정규 CLI가 첫 실패에서 중단된 뒤 남은 미실행 항목을 별도 도구로
확인한 기록입니다. 단일 CLI 완주로 표시하지 않습니다. 4096은 기존 실패 3건만
각 1회 확인한 별도 조건이며, 이를 기존 결과에 합쳐 새 14/15 점수로 쓰지 않습니다.
`finish_reason=length`는 정보 부족 보류가 아니라 응답 실패입니다.

검토 자료는 `v2-gemini-20261014.zip`, `v2-local-followup-20261015.zip`,
`v2-local-limit4096-20261015.zip`입니다. 로컬 전체 확인 ZIP은 초기 `summary.json`
대신 `final-summary.json`이 최종 집계입니다. 원문·프롬프트·설정·입력 해시와
smoke·진단·정규 실행 구분을 함께 확인합니다.

프롬프트 해시는 `8c5975944151d7d8e6ed0f0b4a92fb715e15f38ffe1f63f2b2760bf0ec90b518`로
고정합니다. 응답 실패 시 중단/계속, 생성 한도, 재시도, 긴 본문 처리와 보류 표현의
팀 최종 정책은 검토 대상입니다. 기본 생성 한도는 2048을 유지하며 4096 확인을
기본값 채택으로 해석하지 않습니다. v2 결과 검토와 정책 합의 전에는 병합하지 않습니다.
