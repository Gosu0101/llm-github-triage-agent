# 고정 v2 후속 분류 검토

형언이의 v1 검토에 따른 최소 3문장 변경을 별도 v2 프롬프트로 보존합니다.
v1 실행기·프롬프트·입력·정답·과거 결과는 바꾸지 않습니다. v2 이후에는
이 dev 15건에 맞춰 추가 튜닝하지 않고 별도 9차시 eval에서 판단합니다.

## 묶음과 오프라인 검증

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
첫 실행 실패에서 기록과 요약을 저장하고 중단하며 종료 코드는 2입니다.
부분 실행은 완료된 성능 비교로 해석하지 않습니다. 결과 ZIP에는 v1·v2 원문과
환경 기록을 함께 포함해 프롬프트·설정 확인이 가능하도록 합니다.
