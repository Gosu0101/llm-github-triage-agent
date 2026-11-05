# 저장된 수집 JSON을 기능 입력으로 준비

`feature_inputs`는 기존 수집기의 JSON에서 유형 분류·우선순위·중복 후보용 Issue 입력과 PR 요약 입력을 만듭니다. 네트워크·모델·인증정보 없이 실행하며 원본 파일을 보존합니다. 중복 후보 검색과 PR 요약 모델은 담당 구현을 연결합니다.

## 검증과 저장

설치한 프로젝트 루트에서 실제 수집 파일 경로를 지정합니다. `--output`을 생략하면 검증 결과만 출력하고 파일을 만들지 않습니다.

```powershell
$collection = 'data/oauth/github-collection-20260927T153402Z.json'
python -B -m triage_agent.feature_inputs --collection $collection

$prepared = "data/llm/prepared-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
python -B -m triage_agent.feature_inputs --collection $collection --output $prepared
```

새 출력 폴더에 다음 파일을 저장합니다. 기존 출력 폴더는 덮어쓰지 않습니다.

| 파일 | 내용 |
|---|---|
| issues.json | 일반 Issue가 있으면 생성. priority-preparation-inputs-v0의 cases |
| prs.json | PR이 있으면 생성. pr-summary-inputs-v0의 cases |
| manifest.json | 원본 파일 SHA·저장소·건수·null 본문 변환 목록·개발용 범위 |

`metadata.repository`가 있는 수집 파일 하나를 받으며 각 항목은 해당 저장소에 속해야 합니다. Issue와 PR의 번호 중복, 잘못된 필드·동일 파일 중복 등은 출력 폴더 생성 전에 거부합니다. 빈 수집 자료도 거부합니다. 일반 Issue는 `is_pull_request=false`로 명시돼 있어야 합니다.

## 입력과 원본의 구분

- Issue의 의미 입력은 원형 `title`, `body`만 선택합니다. PR 입력은 `number`, `title`, `body`, `files`이며 파일마다 `filename`, `patch`, `patch_truncated`만 선택합니다. 기존 [결과 연결 계약](feature-report.md)과 같은 순서로 입력 해시를 계산합니다.
- 케이스 ID는 Issue `I번호`, PR `P번호`, item_id는 `저장소#번호`입니다. 원본 라벨·매핑 정답·URL·상태·사용자 메타데이터는 모델 입력에 넣지 않습니다. 정답·split·예측도 생성하지 않습니다. 원본 본문 안의 자연어는 편집하지 않습니다.
- GitHub의 null 본문은 빈 문자열로 정규화하고 해당 item_id를 manifest에 기록합니다. 원본의 문자를 새로 자르지 않습니다. 현재 Issue 입력의 12,000자 제한을 넘으면 거부합니다.
- PR patch의 null과 수집 때의 잘림 표시는 그대로 보존합니다. 빈 파일 목록도 보존하며 후속 보고서에서는 누락/잘림 경고로 확인합니다. PR 전체 토큰 예산과 lock/generated 파일 선별은 후속 모델 입력 단계에서 별도로 적용해야 합니다.
- 이 변환은 개발용 준비입니다. 동결 dev15·예비3·기존 실행 원형을 교체하거나 새 eval을 정의하지 않습니다. 현재 분류 v2 문안은 VS Code 대상이므로 다른 저장소의 분류에 그대로 사용하지 않습니다.

## 기존 실행기·보고서에 연결

Issue가 있는 경우 `issues.json`을 [두 단계 실행기](triage-pipeline.md)의 `--inputs`에 사용할 수 있습니다. 기본은 호출 없는 계획 검증입니다. 준비만 된 상태에서는 분류·우선순위 결과가 생기지 않습니다.

통합 실행기는 원문에 두 문안 중 더 긴 것을 더한 길이도 12,000자 이하여야 합니다. 변환 성공이 모든 모델 호출 준비 완료를 뜻하지는 않습니다. 이 추가 검증에 걸린 항목도 변환 단계에서 임의로 자르거나 제외하지 않습니다. 예를 들어 기존 9/27 수집30건의 `#333377`은 제목·본문9,805자와 우선순위 문안2,402자를 합쳐12,207자라 현재 통합 실행에서 거부됩니다. 이30건의 보고서 입력 검증과 통합 호출 거부는 구분합니다.

이 경우 통합 실행기는 `reason=prompt_input_too_long`, `case_id=I333377`, `excess_chars=207`을 stderr의 진단 JSON으로 알려줍니다. 원문이나 API 키를 출력하지 않고 요청0회·결과폴더 미생성 상태로 종료합니다. 자세한 필드는 [길이 초과 안내](triage-pipeline.md#사용자가-새-실행을-선택할-때)를 따릅니다.

```powershell
python -B -m triage_agent.triage_pipeline --inputs "$prepared/issues.json"
python -B -m triage_agent.feature_report --issue-inputs "$prepared/issues.json"
```

PR이 있는 경우에는 다음과 같이 검증합니다. 실제 PR 요약 결과를 받으면 별도로 `--pr-summaries`를 연결합니다.

```powershell
python -B -m triage_agent.feature_report --pr-inputs "$prepared/prs.json"
```

Issue와 PR을 모두 포함한 수집 파일이면 두 입력 옵션을 함께 지정할 수 있습니다. 중복 판정에는 해당 Issue 입력들과 대상/후보 식별자·해시가 필요하며, 준비한 모든 Issue를 중복 후보로 판정한 것으로 간주하지 않습니다. 댓글 본문·연결 이슈·커밋 메시지·CI 결과는 이번 입력의 필수 필드가 아니며 원본에 없어도 임의로 채우지 않습니다.
