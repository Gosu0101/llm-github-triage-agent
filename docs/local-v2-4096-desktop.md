# 데스크탑에서 로컬 v2 4096 재실행

형언의 v2 검토와 세웅의 재확인에 따른 후속 실험 안내입니다.
노트북에서는 코드·묶음 검증까지만 준비했고 실제 모델 호출은 하지 않았습니다.
**모델 실행은 LM Studio가 설치된 데스크탑에서 진행합니다.**

## 1. 코드와 입력 묶음 준비

PR #22의 `codex/classification-v2-handoff` 브랜치를 사용합니다.
로컬 반복 지원은 커밋 `4c9eac6`부터 포함됩니다. PR은 아직 미병합이므로
기존 main이나 예전 `run_local.py`로 이번 실험을 실행하지 않습니다.

프로젝트 루트에서 작업 중인 변경을 확인하고 보존한 뒤 브랜치를 갱신합니다.
브랜치 전환이 실패하면 현재 변경을 덮어쓰거나 강제 초기화하지 않습니다.

```powershell
git status --short
git fetch origin
git switch codex/classification-v2-handoff
git pull --ff-only
git log -1 --oneline
python -m pip install -e .
```

기존 공유 파일 `classification-review-v2-bundle-20261014.zip`의 **13개 파일을
프로젝트의 `data/classification-review-v2/` 바로 아래**에 준비합니다.
해당 폴더에 `preflight.json`, `v2-preflight.json`, 두 프롬프트, `inputs/`,
`reference/`, `local-settings.json`이 있어야 합니다. ZIP 파일을 놓기만 하거나
바깥 폴더를 한 겹 더 넣지 않습니다. 이미 같은 묶음이 있다면 오프라인 검증부터 합니다.

- ZIP SHA-256: `7a622182e5d42e9b86ee6cc26ec8424f7967d3787e904b13d8fda4decf865bbb`
- 입력 파일 SHA-256: `d5da734f488daf4f5ef365696b26583b09e55ac2f02b0a6b33b0f70fd883ccfc`
- v2 프롬프트 SHA-256: `8c5975944151d7d8e6ed0f0b4a92fb715e15f38ffe1f63f2b2760bf0ec90b518`

데이터 묶음은 Git에 포함되지 않으므로 코드를 갱신해도 자동으로 생기지 않습니다.
입력·정답·프롬프트·manifest를 이번 점수에 맞추려고 수정하지 않습니다.

## 2. 오프라인 검증 — 모델 요청 0회

프로젝트 루트에서 다음 명령을 실행합니다. 환경변수나 Gemini 키는 필요하지 않습니다.

```powershell
python -B -m triage_agent.review_v2 --provider local --max-tokens 4096 --timeout 600 --boundary-repeats 3
```

성공하면 `mode=validated`, `dev_items=15`, `reserve_items=3`,
`planned_calls=24`, `network_calls=0`이 출력됩니다.
검증이 실패하면 실제 실행을 진행하지 말고 오류 메시지와 종료 코드를 보존합니다.
[호출 전 거부 기록 절차](classification-execution-policy-draft.md#호출-전-검증-거부의-기록--2026-10-18-보완)를 따릅니다.

## 3. LM Studio 설정 확인

이전 실험과 같은 모델·나머지 조건을 사용합니다.

| 항목 | 설정 |
|---|---|
| LM Studio | 이전 기록 0.4.25 Build 1. 실제 버전을 확인·기록 |
| 모델 ID | `supergemma4-26b-uncensored-v2` |
| 모델 파일 | `supergemma4-26b-uncensored-fast-v2-Q4_K_M.gguf` |
| Context / GPU Offload | 8192 / 15 |
| Thinking | 켜짐·무제한(Unrestricted) |
| 요청 조건 | temperature 0, strict JSON, 자동 재시도 0회 |
| 이번 변경 | 생성 한도 **4096**, timeout **600초** |

로컬 서버를 시작하고 다음 명령으로 모델 ID를 확인합니다. 이 조회는 모델 생성 요청이 아닙니다.

```powershell
Invoke-RestMethod 'http://127.0.0.1:1234/v1/models' | ConvertTo-Json -Depth 5
```

**입력 토큰 + 최대 생성 4096이 Context 8192 안에 들어가는지도 실행 전에 확인합니다.**
입력 토큰에는 서버의 채팅 템플릿·시스템 프롬프트가 포함될 수 있습니다.
형언의 기존 dev 검토는 최대 입력 2,975토큰을 보고했지만 현재 로딩 상태와
템플릿까지 자동 검증한 것은 아닙니다. CLI의 12,000자 검사는 토큰 검사가 아닙니다.
토큰 적합성을 확인하지 못한 상태를 확인 완료로 기록하지 않습니다.

실제 로딩 설정은 화면 캡처나 별도 환경 메모로 남깁니다. 묶음의
`local-settings.json`은 과거 기록이므로 현재 환경이 자동 확인됐다는 증거가 아닙니다.
버전·설정이 다르면 차이를 기록하고 동일 조건인지 검토한 뒤 진행합니다.

## 4. 정규 18건 + 경계 추가 6회 실행

아래 코드는 매번 새 결과 경로를 만들고 실제 모델 요청을 보냅니다.
별도 smoke를 추가하지 않는 한 정상 완주 계획은 **24회**입니다.

```powershell
$reviewRun = Join-Path 'data/llm/review' ('v2-local-4096-' + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ') + '-' + [guid]::NewGuid().ToString('N').Substring(0,8))
python -B -m triage_agent.review_v2 --provider local --execute --max-tokens 4096 --timeout 600 --boundary-repeats 3 --output $reviewRun
$reviewExitCode = $LASTEXITCODE
Write-Output "Exit code: $reviewExitCode"
Write-Output "Results: $reviewRun"
```

- `results/`: dev 15건과 예비 3건
- `stability/`: `#331658`·`#338087` 각각 **추가 3회**. 정규 포함 각 4회입니다.
- `run-manifest.json`: max_tokens 4096, timeout_seconds 600, 반복 3회 및 기존 조건 기록.
- `summary.json`: primary_calls 18, repeat_calls 6, transport_attempts 24, stopped null이면 정상 완주.
- `v1-v2-comparison.csv`, v1/v2 프롬프트 원문, `local-settings.json`도 함께 보존.

CLI 기본값은 여전히 **2048·300초·로컬 추가 반복 0회**입니다.
이번 명령의 명시적 옵션으로만 4096·600초·반복 3회를 적용합니다.

호출 단계에서 실패 결과가 나오면 해당 기록과 summary를 저장하고 종료 코드 2로
중단합니다. 실패했다면 완료 횟수와 미실행 범위를 확인하고 그대로 보존합니다.
현재 CLI에는 자동 재개 기능이 없습니다. 4096에서도 잘리면 실패로 남기고 한도를
계속 올리거나 성공 결과만 골라 합치지 않습니다.

## 5. 결과 확인과 ZIP 전달

같은 PowerShell 창에서 다음을 확인합니다.

```powershell
Get-Content -LiteralPath (Join-Path $reviewRun 'summary.json') -Raw
Get-Content -LiteralPath (Join-Path $reviewRun 'run-manifest.json') -Raw
```

공식 dev 점수는 정규 **15건만**으로 계산합니다. 예비 3건과 반복 6건은 별도
보고하며 반복의 정답으로 정규 예측을 교체하지 않습니다. 이전 2048의
**12/15·잘림 3건**, 실패 3건만 확인한 4096 **2/3**도 그대로 보존합니다.

응답의 `usage`(현재 클라이언트는 prompt/completion/total 토큰), `finish_reason`,
지연·시도 내역을 함께 확인합니다. 사고 토큰의 상세 분리가 필요하면 LM Studio의
해당 실행 로그도 보존합니다. 서버가 제공하지 않은 사용량을 만들어 넣지 않습니다.
실패 응답에는 현재 구현상 usage가 남지 않을 수 있으므로 로그와 구분해 보고합니다.

결과 폴더 전체를 ZIP으로 묶고 현재 로딩 설정 기록·실행한 코드 커밋을 함께 전달합니다.
결과 폴더가 만들어지지 않은 호출 전 거부라면 검증 기록을 전달합니다.

```powershell
Compress-Archive -LiteralPath $reviewRun -DestinationPath ($reviewRun + '.zip')
```

형언이 새 결과로 최종 비교표를 검토한 뒤 기본값 변경을 판단합니다.
결과와 관계없이 v2 프롬프트·정답 유지와 dev 튜닝 종료 원칙은 같습니다.

## 검토 근거

- [형언의 v2 결과 검토](https://github.com/Gosu0101/llm-github-triage-agent/issues/13#issuecomment-6257488787)
- [세웅의 문서 재확인·후속 실험 조건 동의](https://github.com/Gosu0101/llm-github-triage-agent/pull/22#issuecomment-6259026196)
- [v2 실행·결과 구분](classification-v2-review.md) · [실행 정책](classification-execution-policy-draft.md)
