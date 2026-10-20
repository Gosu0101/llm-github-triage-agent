# 우선순위 임시 계약·오프라인·모의 실행·LM Studio 연결

`python -B -m triage_agent.priority_review`는 기본적으로 인증정보·서버 없이 입력과 대상 계획만 검증합니다. `--mock-responses`는 모의 저장, **`--execute`는 LM Studio 실제 호출**입니다. 두 실행 모드는 함께 사용할 수 없습니다. 현재 실제 연결 제공자는 로컬 LM Studio이며 Gemini용 우선순위 어댑터는 아직 없습니다.

출력 계약은 `priority-bug-v0-draft`, 모델에 주는 문안은 `priority-bug-v0.1-draft`입니다. bug 한정·네 필드·별도 호출에 대한 두 검토자 동의와 사용자의 G05 medium 선택을 반영한 초안입니다. 반복 지장이 본문에 직접 명시되면 medium을 줄 수 있고, 완료 가능·우회 성공을 확인된 사실로 추정하지 않습니다. 기존 분류 v2 프롬프트·클라이언트·기본값은 변경하지 않습니다.

문안 원문은 패키지에 포함된 `src/triage_agent/prompts/priority-bug-v0.1-draft.txt`입니다. 읽은 문안의 줄바꿈을 LF로 정규화한 UTF-8 SHA-256은 `dd074db28862a2b19b4eee62561a0752a0e2da18bb68d690a101e37348491f69`이며 실행 전 확인하고 결과 폴더에도 같은 원문을 저장합니다. 실제 모델 품질과 high 문구·평가 지표 전체가 검증·확정된 것은 아닙니다.

## 입력·분류 결과 준비

공유된 `priority-preparation-inputs-20261020.json`을 사용합니다. 코드 저장소에는 실제 원문 묶음·사람 정답·과거 결과를 넣지 않습니다. 예를 들어 로컬 `data/llm/priority/inputs.json`에 보관할 수 있습니다. `data/llm/`는 Git에서 제외됩니다.

입력 파일은 `schema_version: "priority-preparation-inputs-v0"`와 `cases` 배열을 가지며 각 행은 다음 구조입니다.

```json
{
  "case_id": "G01",
  "item_id": "synthetic/G01",
  "origin": "synthetic_existing_conditions_generated_title",
  "input": {"title": "가상 버그 사례 G01", "body": "보고 원문"},
  "input_sha256": "해당 input의 실제 SHA-256"
}
```

해시는 `json.dumps({'title': title, 'body': body}, ensure_ascii=False).encode('utf-8')`의 SHA-256입니다. 파일의 키 순서와 관계없이 **title, body 순서로 직렬화**합니다. input의 키는 두 개이고 모두 문자열이어야 합니다. ID 중복·추가 입력 키·불일치 해시·12,000자를 넘는 title+body는 호출 전에 거부합니다. case_id의 대소문자만 다른 중복과 Windows 예약 파일명(CON/AUX/COM1 등)도 거부합니다. 로컬 계획·실행은 추가로 프롬프트+대상 title/body의 12,000자 한도를 확인합니다. 이 문자 제한은 모델의 토큰 컨텍스트 검증을 대신하지 않습니다.

상위 분류 자료는 기존 실행의 **정규 `results/` 디렉터리** 또는 건별 결과 JSON을 한 줄씩 담은 JSONL입니다. 반복 `stability/`, gold 파일·원본 라벨은 사용하지 않습니다. 성공 기록에는 `item_id`, `input_sha256`, `status=success`, 유효한 `prediction.type/rationale/needs_review`가 필요합니다. 실패 기록은 알려진 실패 상태와 `prediction=null`이어야 합니다. 입력과 겹치는 기록은 해시를 확인하고 나머지는 `ignored_classifications`로 집계합니다. 기록의 해시는 내용 일치 확인이며 출처의 진실성이나 모델의 분류 정확도를 보증하지 않습니다.

가상 10건은 실제 분류 결과가 없으므로 자동으로 bug 처리하지 않습니다. 테스트에서 만든 분류 기록은 모의 자료라고 표시해야 합니다.

## 오프라인 검증

프로젝트 루트에서 `pip install -e .`로 설치한 뒤 실행합니다.

```powershell
python -B -m triage_agent.priority_review --inputs 'data/llm/priority/inputs.json'
```

상위 결과가 없으면 준비 입력 15건은 모두 `not_ready`, eligible 0입니다. 정상적인 검증 결과이며 모델 보류가 아닙니다.

```powershell
python -B -m triage_agent.priority_review --inputs 'data/llm/priority/inputs.json' --classifications 'data/llm/review/기존실행/results'
```

기본 실행은 계획만 출력하며 파일을 생성하지 않습니다. `network_calls=0`, `credentials_required=false`입니다. `planned_calls`는 대상 수일 뿐 실제 요청 수가 아닙니다. 실제 계획은 입력과 일치하는 유효한 분류 결과에 따라 정합니다.

| 상위 분류 | 처리 |
|---|---|
| 성공 bug·보류 아님 | eligible |
| 성공 question/feature-request | not_applicable |
| 성공 type=null·needs_review=true | classification_review |
| 알려진 실패 상태·prediction 없음 | upstream_failed |
| 분류 기록 없음 | not_ready |
| 겹치는 입력의 해시 불일치·잘못된 예측 조합·중복 ID | 검증 실패, exit 2 |

## 데스크탑에서 LM Studio 실제 실행

현재 사용자의 랩탑에서는 실제 호출을 수행하지 않았습니다. 데스크탑에 이 작업 브랜치의 코드와 공유 입력·상위 분류 결과를 준비한 뒤 실행합니다. 로컬 커밋만 있는 상태라면 다른 기기에 자동으로 전달된 것으로 보지 않습니다.

초기 우선순위 연결 설정은 `supergemma4-26b-uncensored-v2`, 생성 한도4096·timeout600초·temperature0·재시도0·stream=false입니다. 이는 우선순위 실행기의 초기값이며 **분류 PR #22의 기본2048·300초를 바꾼 것이 아닙니다.** LM Studio에서 실제 로딩 모델·양자화·GPU Offload·context 설정을 확인합니다.

```powershell
# 요청 없이 첫 eligible bug 1건의 계획을 확인
python -B -m triage_agent.priority_review --inputs 'data/llm/priority/inputs.json' --classifications 'data/llm/review/기존실행/results' --smoke --max-tokens 4096 --timeout 600 --context-length 8192

# LM Studio에서 실제 context와 입력 토큰 적합성을 확인한 뒤, 새 폴더로 1건 실행
python -B -m triage_agent.priority_review --inputs 'data/llm/priority/inputs.json' --classifications 'data/llm/review/기존실행/results' --execute --smoke --output 'data/llm/priority/local-smoke-01' --max-tokens 4096 --timeout 600 --context-length 8192
```

`--context-length`는 **사용자가 보고한 값의 기록**이며 서버 설정을 읽거나 바꾸지 않습니다. 로딩 context가8192이고 생성 예산4096이면 실제 모델 입력 토큰+4096이8192 안에 들어가는지 실행 PC에서 확인합니다. 문자 수 통과만으로 토큰 적합성이 확인되었다고 보지 않습니다. 모델이 다르면 `--model`, 포트가 다르면 `--base-url`을 지정하되 endpoint는 localhost/127.0.0.1/::1의 HTTP `/v1`만 허용합니다.

1건의 응답·근거·토큰·지연·잘림 상태를 확인한 뒤 새 폴더에서 `--smoke`를 빼면 준비된 eligible 전체를 실행합니다. 원래 입력15건 중 가상10건에 분류 결과가 없으면 실제5건만 대상입니다. `--smoke`는 첫 대상만 결과에 담고 manifest에 `source_total`과 `selection=smoke`를 기록하므로 전체15건 실행이나 점수가 아닙니다.

실행 후 `summary.json`의 `network_calls`는 **요청 시도 수**이며 모델이 정상 응답한 수와 다릅니다. 실패 요청도 포함합니다. `simulated_calls`는0이고 manifest·건별 provider는 `lm-studio-local`입니다. 서버 요청에는 기준을 담은 system 메시지와 title/body만 직렬화한 user 메시지를 보냅니다. 상위 분류 근거·ID·정답은 메시지에 넣지 않습니다.

## 저장까지 확인하는 모의 실행

`--mock-responses` 파일은 **eligible 순서대로** 한 행씩 담은 JSON 배열입니다. 각 행은 `case_id`, `input_sha256`, `response` 세 키만 가지며, 앞의 두 값이 입력 계획과 일치해야 합니다. 응답 수·순서·해시가 틀리면 결과 폴더 생성 전에 거부합니다.

```json
[
  {
    "case_id": "G01",
    "input_sha256": "eligible 입력의 실제 SHA-256",
    "response": {
      "choices": [{
        "finish_reason": "stop",
        "message": {
          "content": "{\"priority\":null,\"rationale\":\"모의 보류 응답이며 실제 판단이 아니다.\",\"needs_review\":true,\"missing_information\":[\"필요한 작업을 완료할 수 있나요?\"]}"
        }
      }],
      "usage": {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50}
    }
  }
]
```

위 값은 형식 예시이며 실제 해시와 eligible 항목 수에 맞게 준비해야 합니다. usage도 예시 수치입니다.

```powershell
python -B -m triage_agent.priority_review --inputs 'data/llm/priority/inputs.json' --classifications 'data/llm/review/기존실행/results' --mock-responses 'data/llm/priority/mock-responses.json' --output 'data/llm/priority/mock-run-01'
```

`--mock-responses`와 `--output`은 함께 지정합니다. 출력 폴더는 새 경로여야 하며 기존 폴더를 덮어쓰거나 이어서 실행하지 않습니다. 결과는 다음과 같습니다.

- `run-manifest.json`: mode/provider는 mock, 임시 계약 버전, 입력·상위 결과·모의 응답 파일 해시, 자동 재시도 0. 로컬 실제 실행에서는 mode=local, provider=lm-studio-local과 모델·설정·timeout·보고된 context·프롬프트 버전/해시를 기록.
- `results/<case_id>.json`: 원래 입력당 1개. 제외·누락·미실행도 상태와 prediction 없음으로 기록.
- `summary.json`: 전체·대상·모의 요청·성공·보류·실패·미실행·제외 상태·등급 분포. 실제 요청은 0회. 정확도/F1 없음.

등급은 high/medium/low 또는 JSON null입니다. 네 필드가 모두 있어야 하며 추가 키는 거부합니다. null이면 needs_review=true와 공백 아닌 질문 1~3개, 등급이 있으면 false와 빈 질문 배열이어야 합니다. 근거도 공백이면 안 됩니다. 문장의 의미·한국어 품질·질문의 유용성은 이 타입 검증이 보증하지 않습니다.

유효한 보류는 success로 저장하고 계속 진행합니다. 응답 잘림·JSON/스키마 오류·전송 실패를 보류로 바꾸지 않습니다. 첫 실패를 저장하고 이후 eligible은 unattempted로 남긴 뒤 summary를 저장하며 exit 2로 끝납니다. 이미 제외된 항목은 실패 이후에도 제외 상태를 유지합니다. 실제 통신 예외는 테스트의 주입 함수로 검증하고 CLI 파일에는 원시 예외나 인증정보를 넣지 않습니다.

## 확인 범위와 다음 연결

테스트는 `python -B -m unittest discover -s tests -v`로 실행합니다. Windows sandbox의 임시 폴더 권한 오류가 나면 TMP/TEMP를 쓸 수 있는 프로젝트 밖 시험 폴더로 지정한 뒤 다시 실행합니다.

LM Studio 어댑터와 공통 파싱·저장·중단 흐름을 테스트에서 모의 HTTP 경계로 검증했습니다. 원문 프로토콜은 기존 로컬 전송 함수를 사용하며, 이 함수가 시간 초과를 포함한 통신 오류를 network_error로 정규화할 수 있습니다. 잘림·JSON/스키마 오류와 구분해 기록하고 자동 재시도하지 않습니다.

실제 LM Studio 스모크/전체 실행·모델 품질·토큰 적합성, Gemini 어댑터, 전체 사람 정답·평가 분모는 후속입니다. 모의 응답을 사람이 매긴 우선순위나 실제 모델 결과로 재사용하지 않습니다. G05 medium 선택과 R01/R05 보류 판단은 문안의 근거이며 실제 모델이 그렇게 출력했음을 뜻하지 않습니다.
