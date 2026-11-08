# 저장된 이슈의 중복 후보 검색과 판정

`duplicate_detection`은 준비한 `issues.json`에서 후보를 검색하고, 실행을 명시하면 Gemini로 대상·후보 쌍을 판정합니다. `feature_inputs`로 만든 파일과 기존 우선순위 준비 입력을 그대로 사용합니다.

## 호출 없이 후보 확인

```powershell
$inputs = '<공유 ZIP을 푼 폴더>/microsoft-vscode/prepared/issues.json'
python -B -m triage_agent.duplicate_detection --inputs $inputs --target 'microsoft/vscode#323414'
```

후보는 같은 저장소에서 이슈 번호가 더 작은 항목으로 제한합니다. 현재 입력에는 작성 시각이 없어 번호를 순서의 기준으로 사용합니다. 제목과 제목·본문 전체의 단어 겹침을 각각 구하고 제목에 두 배 가중치를 줍니다. 점수가 같으면 이슈 번호가 큰 후보부터 제시합니다. 기본 최대 3건이며 `--limit`으로 1~10건을 선택하거나 `--candidate 'microsoft/vscode#322876'`으로 한 쌍을 직접 지정할 수 있습니다. 이 점수는 검색 순위에만 쓰입니다. 검색 범위는 저장한 묶음이며 단어가 다른 유사 이슈를 놓칠 수 있습니다.

검증은 인증정보와 파일 쓰기 없이 후보 식별자·입력 해시·예정 호출 수를 보여줍니다. 후보가 없으면 `no_candidates`와 0회가 표시됩니다. 지정된 후보의 문안과 JSON 입력 합계가 12,000자를 넘으면 호출 전에 오류를 표시합니다.

## 한 쌍 실행 후 보고서 연결

프로젝트를 설치하고 `GEMINI_API_KEY`를 준비합니다. 예시는 PR 요약에서 호출이 확인된 `gemini-3.6-flash`를 사용합니다. 중복 판정의 품질은 별도로 평가합니다.

```powershell
$run = "data/llm/duplicate/vscode-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
python -B -m triage_agent.duplicate_detection --inputs $inputs --target 'microsoft/vscode#323414' --candidate 'microsoft/vscode#322876' --execute --env-file '.env' --output $run
python -B -m triage_agent.feature_report --issue-inputs $inputs --duplicates "$run/results" --output "$run/report"
```

Gemini에는 대상·후보의 번호·제목·본문만 전달합니다. 실행기는 선택한 쌍과 해시를 `run-manifest.json`에, 각 판정과 실패를 `results/`에 저장합니다. 자동 재시도는 0회이며 첫 실패 후 남은 쌍은 `unattempted`로 기록합니다. 실행 결과는 [기존 중복 결과 계약](feature-report.md#중복-결과-연결)을 따릅니다. 분류·우선순위 결과가 없는 이슈는 중복 판정이 성공해도 종합 상태가 일부 연결로 남을 수 있습니다. 개발용 두 이슈는 평가 정답이나 확정 중복 쌍으로 사용하지 않습니다.
