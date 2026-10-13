# 고정 dev 분류 실험 실행기

기존 `stepup-v1-review` 실행기를 프로젝트 모듈로 정리한 개발용 도구입니다.
영어 VS Code dev 15건 + 예비 3건의 고정 묶음을 검증하고, Gemini v1 또는
LM Studio 로컬 v0/v1을 실행합니다. 일반 Issue를 실시간 분류하거나
GitHub 라벨·댓글을 게시하는 도구가 아닙니다.

## 준비

프로젝트 루트에서 Python 3.11 이상으로 설치합니다.

```powershell
python -m pip install -e .
```

고정 데이터 묶음은 기본적으로 프로젝트의 **`data/classification-review/`**에 둡니다. 현재 작업 PC에는 기존 `stepup-v1-review` 묶음의 필수 8개 파일을 해시 변경 없이 복사했습니다. 다른 PC에서도 팀이 공유한 동일 묶음을 이 위치에 준비해야 합니다. Git에서는 제외되므로 clone만으로 데이터가 전달되지는 않습니다. 다른 위치를 쓰려면 `--bundle`로 지정합니다.
묶음에는 다음 파일이 필요합니다. 결과 ZIP만으로는 실행할 수 없습니다.

- `preflight.json`
- `classification-v1-review.txt`
- `inputs/dev-inputs.jsonl`, `inputs/reserve-inputs.jsonl`
- `reference/dev-gold.jsonl`, `reference/v0-results.jsonl`
- `reference/run-manifest.v0.json`, `reference/classification-v0-review.txt`

원문·정답·결과는 저장소에 커밋하지 않습니다. 프로젝트 안에 두려면
Git에서 제외한 `data/classification-review/`를 사용합니다. 팀원별 외부
폴더를 지정해도 됩니다. 코드에 특정 PC의 경로를 고정하지 않습니다.

`prompts/`에는 당시 v0/v1의 원형을 기록합니다. 실행은 `--bundle` 안의
프롬프트를 사용하며 manifest의 해시로 검증합니다. 묶음을 수정해서
이번 결과를 대체하지 않습니다. 다음 버전의 일반화된 평가 실행기는 별도 작업입니다.

## 1. 환경값 없이 먼저 검증

```powershell
python -B -m triage_agent.classification_review
python -B -m triage_agent.classification_review --provider local
```

프로젝트 루트에서 위 명령만 실행하면 기본 묶음을 읽습니다. 외부 묶음은 `--bundle '다른 폴더'`로 선택할 수 있습니다. 기본 모드는 API 요청 0회이며 `.env`를 읽지 않습니다. 파일 해시,
항목 수·ID, title/body 전용 입력, 정답·기존 결과 연결을 검증합니다.
잘림·정답 필드 포함·변조가 발견되면 실행 전에 거부합니다.

## 2. Gemini v1 실행 — 18건

`GEMINI_API_KEY`가 설정된 환경 또는 사용자가 지정한 `.env`를 사용합니다.
키 값을 로그·명령 인자·공유 문서에 작성하지 않습니다. 기존 환경변수가
환경파일보다 우선하며 키의 프로젝트 연결은 사용자가 확인해야 합니다.

```powershell
python -B -m triage_agent.classification_review --execute --env-file .env
```

- 기존 프로젝트 `GeminiClient`를 사용합니다. `--project` 인자는 필요하지 않습니다.
- model/timeout/output/input/retry 설정은 고정 묶음의 manifest에서 읽습니다.
- 호출 시작 간격은 기본 13초, 재시도 요청에도 적용됩니다. `--interval`은
  0~60초 범위에서 조절할 수 있습니다. API의 일일 한도를 해결하는 옵션은 아닙니다.
- dev 15건 + 예비 3건을 기존 Gemini v0 결과와 비교합니다. Gemini v0를 새로 실행하지 않습니다.
- 인증 오류·429·402는 저장 후 중단합니다. 다른 최종 실패도 별도로 집계하며
  종료 코드는 2입니다. 실패를 분류 유형이나 보류로 바꾸지 않습니다.
- 지연시간에는 pacing 대기가 포함될 수 있어 순수 추론 속도로 해석하지 않습니다.

## 3. LM Studio 로컬 실행 — 1건 확인 → 33건 비교

LM Studio에서 서버를 시작하고 정확한 모델 ID를 확인합니다. 앞선 실험은
`supergemma4-26b-uncensored-v2`, Context Length 8192를 사용했습니다.
컨텍스트는 서버의 로딩 설정이고 `--max-tokens`는 생성 한도입니다.

```powershell
python -B -m triage_agent.classification_review --provider local --execute --smoke
python -B -m triage_agent.classification_review --provider local --execute
```

로컬 기본값: `http://127.0.0.1:1234/v1`, temperature 0, max_tokens 2048,
timeout 300초, strict JSON Schema, 자동 재시도 0. `--model`, `--base-url`,
`--max-tokens`, `--timeout`으로 조절할 수 있습니다. endpoint는 localhost만 허용합니다.
로컬 실행은 환경파일·API 키를 읽지 않습니다.

전체 실행은 v0 dev 15건과 v1 dev 15건+예비 3건입니다. 비교 기준은 **같은
로컬 모델의 v0 재실행**이며 Gemini 결과를 로컬의 기준값으로 사용하지 않습니다.
첫 실패에서 저장 후 중단하고 종료 코드 2를 반환합니다. length 종료나
비정상 JSON을 성공으로 받아들이지 않습니다. smoke는 v1 첫 dev 항목 1건만
실행하며 전체 정확도·비교 지표를 주장하지 않습니다.

## 결과·재현·팀 검토

기본 결과는 Git에서 제외된 `data/llm/review/<provider>-<UTC>-<ID>/`에 저장합니다.
`--output '새 폴더'`로 위치를 바꿀 수 있으며 기존 폴더는 덮어쓰지 않습니다.
이전 폴더를 재개하거나 실패 항목만 자동 재시도하는 기능은 없습니다.

- Gemini: `results/`, `run-manifest.json`, `summary.json`, `dev-comparison.csv`,
  `attempt-history.jsonl`, `execution-notes.json`.
- 로컬: `v0/`, `v1/`, `run-manifest.json`, `summary.json`, 모델별 비교 CSV.
- 보류·실패·미실행도 dev 전체 15건 분모에 남기며 예비 3건은 별도 집계합니다.
- `null / needs_review`, 재시도·긴 본문 규칙은 이 실험 설정이며 팀 최종 합의가 아닙니다.
- LM Studio 버전·양자화·GPU Offload·Context Length는 별도 기록해야 합니다.
- 결과 ZIP에 원문은 없으므로 검토 담당자에게 고정 입력 자료도 제공해야 합니다.
- API 호출 없이 개발용 테스트: `python -B -m unittest discover -s tests -v`.

실행기 이동은 형언이의 분류 기준 검토·팀 승인·PR 병합·최종 eval 완료를 의미하지 않습니다.
