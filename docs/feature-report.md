# 저장된 네 기능 결과 연결

`feature_report`는 분류·우선순위·중복 판정·PR 요약의 **이미 저장된 결과**를 원본 식별자와 입력 해시로 연결합니다. 모델이나 임베딩을 호출하지 않으며 별도 인증정보도 읽지 않습니다. 세웅 담당 알고리즘의 구현이나 실제 네 기능 추론 완료를 뜻하지 않습니다.

## 기존 Issue 결과부터 확인

설치한 프로젝트 루트에서 기존 입력·분류 결과·우선순위 결과를 지정합니다. 결과 파일은 단일 JSON 객체, JSON 객체 배열 또는 항목별 JSON 파일 디렉터리를 받습니다. 실행 폴더 전체 대신 각 `results/`를 지정합니다.

```powershell
$classifications = 'data/llm/review/v2-local-remaining-20261014T150549Z/first-attempt-results'
$priorities = 'data/llm/priority/gemini-all-v03-20261028-011748/results'
python -B -m triage_agent.feature_report --issue-inputs 'data/llm/priority/inputs.json' --classifications $classifications --priorities $priorities
```

기본은 검증·상태 집계만 출력하고 파일을 만들지 않습니다. 저장하려면 새 경로를 지정합니다.

```powershell
$output = "data/llm/reports/features-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
python -B -m triage_agent.feature_report --issue-inputs 'data/llm/priority/inputs.json' --classifications $classifications --priorities $priorities --output $output
```

기존15입력에서는 실제5건의 분류·우선순위만 연결되고 중복 결과가 없어 partial5, 나머지 가상10건은 not_ready입니다. 과거 전체18분류 중 선택 입력에 없는13개는 `ignored_records.classification`에 집계합니다. 새 모델 실행·새 점수로 해석하지 않습니다. 원본 결과·입력은 덮어쓰지 않습니다.

새 통합 실행기를 사용한 경우 분류는 `<pipeline>/classification/results`, 우선순위는 `<pipeline>/priority/results`를 지정하면 됩니다. 중단된 실행의 unattempted도 그대로 보존합니다.

## 중복 결과 연결

`--duplicates <JSON파일 또는 디렉터리>`를 추가합니다. 각 판정 결과는 기존 [중복 출력 예시](llm-candidates-and-io.md#중복-판정-예시)의6필드를 prediction에 보존하고, 호출 외부의 식별·해시4개를 붙입니다.

```json
{
  "item_id": "microsoft/vscode#15",
  "input_sha256": "대상 title/body 해시",
  "candidate_item_id": "microsoft/vscode#3",
  "candidate_input_sha256": "후보 title/body 해시",
  "status": "success",
  "prediction": {
    "target_issue_number": 15,
    "candidate_issue_number": 3,
    "verdict": "related",
    "confidence": 0.8,
    "evidence": ["같은 기능을 다루지만 발생 조건이 다름"],
    "rationale": "동일 문제로 확정하지 않음"
  }
}
```

위 값은 형식 설명용이며 실행 결과가 아닙니다. 대상·후보의 입력을 모두 Issue 입력 묶음에 포함해야 해시를 확인할 수 있습니다. 같은 저장소의 서로 다른 항목만 허용하며 번호·입력 해시가 일치해야 합니다. 같은 대상/후보 쌍의 결과가 둘 이상이면 임의로 선택하지 않고 거부합니다. 서로 다른 후보의 결과는 목록으로 보존합니다.

`verdict`는 duplicate/related/not_duplicate/insufficient_information, confidence는0~1 유한 숫자입니다. confidence를 중복 확률이나 자동 처리 임계값으로 사용하지 않습니다. 결과가 없는 경우는 not_ready이며 빈 파일을 '중복 없음 확인'으로 바꾸지 않습니다. 일부 후보 결과가 있다고 전체 후보 검색이 끝났다고 표시하지도 않습니다. 실패는 prediction=null과 기존 실행 status로 전달합니다.

## PR 요약 연결

`--pr-inputs <입력묶음> --pr-summaries <결과파일 또는 디렉터리>`를 추가합니다. PR 입력은 Issue용 분류기에 보내지 않습니다. PR 요약의 모델 입력은 기존 문서의 number/title/body/files입니다.

입력 묶음은 `schema_version="pr-summary-inputs-v0"`, cases 배열이며 각 항목은 기존 Issue 묶음처럼 case_id/item_id/origin/input/input_sha256을 가집니다. input은 정확히 number/title/body/files, 각 file은 filename/patch/patch_truncated입니다. patch가 없는 경우 null을 보존합니다. JSON 해시는 다음 순서로 정규화한 객체를 `review_bundle.input_hash()`에 넣어 계산합니다.

```python
value = {"number": number, "title": title, "body": body,
         "files": [{"filename": f["filename"], "patch": f["patch"],
                    "patch_truncated": f["patch_truncated"]} for f in files]}
```

요약 결과도 item_id/input_sha256/status/prediction을 붙입니다. prediction에는 기존 [PR 요약 예시](llm-candidates-and-io.md#pr-요약-예시)의 summary/key_changes/affected_areas/tests/risks/breaking_changes/insufficient_information 7필드를 그대로 넣습니다. 리스트는 문자열 목록이고 insufficient_information은 실제 boolean입니다. 정보 부족=true이면 risks에 부족한 정보를 적습니다.

현재 연결용 wrapper는 이 작업에서 정한 개발 인터페이스이며 팀 전원의 확정 계약으로 간주하지 않습니다. 담당자가 다른 내부 형식을 사용하면 모델 출력 자체를 바꾸지 말고 이 wrapper로 변환하면 됩니다. 모델에 없는 결과를 채우거나 등급·판정을 재작성하지 않습니다.

원본 patch가 잘렸거나 없으면 외부 warnings에 source_patch_truncated/source_patch_missing을 기록합니다. 모델이 insufficient_information=false를 출력했어도 원형 prediction은 유지하고 종합 needs_review=true로 표시합니다. tests 목록이나 빈 breaking_changes만으로 테스트 실행 성공·호환성 보장을 주장하지 않습니다.

## 상태와 저장 범위

`report.json`에는 원문 본문/patch 대신 식별자·해시·기능별 status/prediction과 입력·결과 파일의 SHA를 저장합니다. 모든 원형 결과의 전체 복사본을 대신하는 것은 아니므로 원본을 함께 보존합니다. 단계의 모델·문안 식별자도 있으면 전달합니다.

| 종합 상태 | 의미 |
|---|---|
| not_ready | 해당 대상의 필요한 결과가 아직 없음 |
| partial | 일부 기능 결과 또는 미실행 기록만 있음 |
| failed | 저장된 실행 실패가 있음; 정보 부족 보류와 구분 |
| needs_review | 결과가 연결됐으나 모델 보류·정보 부족 또는 원본 잘림 경고가 있음 |
| ready | 지정한 결과가 연결됐고 기록상 보류·실패·미준비가 없음 |

ready는 정답·전체 후보 검색 완료·네 기능의 실환경 통합 완료를 뜻하지 않습니다. PR에는 분류·우선순위·중복이 not_applicable이고, Issue에는 PR 요약이 not_applicable입니다. 분류 성공·비보류 bug 근거가 없으면 우선순위 성공 결과의 연결을 거부합니다. 입력/대상/후보 해시 불일치, 중복 결과, 잘못된 JSON·필드 조합도 저장 전에 거부합니다. 기존 출력 폴더는 덮어쓰지 않습니다.

## 구현·검증 범위

기존 두 단계 실행기는 그대로 두고 결과를 나중에 결합하는 오프라인 CLI를 선택했습니다. 새6개 테스트를 먼저 작성해 미구현 실패를 확인한 뒤 연결·해시 검증·미준비/실패 분기·PR 원본 잘림 경고를 구현했습니다. 잘못된 confidence 값과 누락된 prediction도 저장 전에 거부하도록 검증했습니다. 실제 중복·요약 모델 결과는 아직 제공되지 않아 해당 분기는 형식 설명용 테스트 입력으로만 확인했습니다.
