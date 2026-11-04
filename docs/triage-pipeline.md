# 유형 분류와 우선순위 통합 실행

일반 Issue 하나를 분류하고, 성공한 bug일 때만 우선순위를 별도 호출한 뒤 두 결과를 함께 저장합니다. 현재 동결 분류 프롬프트는 **VS Code 전용 v2**이고 우선순위는0.3입니다. 다른 제품에 그대로 적용하는 범용 분류기가 아닙니다.

## 설치와 호출 없는 확인

`codex/triage-pipeline` 브랜치의 프로젝트 루트에서 설치합니다. 브랜치를 전환할 때는 기존 변경을 먼저 보존합니다. 기존 공유 입력 또는 최소 인계 ZIP의 `data/llm/priority/inputs.json`을 사용합니다. 코드를 받는 것만으로 공유 입력이 생기지는 않습니다.

```powershell
python -m pip install -e .
python -B -m triage_agent.triage_pipeline --inputs 'data/llm/priority/inputs.json'
```

기본 실행은 키·환경파일·서버 없이 두 프롬프트와 모든 선택 입력의 해시·문자 길이를 검증합니다. 분류 결과 파일을 미리 준비할 필요가 없습니다. `classification_calls`는 선택 입력 수, `priority_calls_max`는 모두 bug일 때의 상한입니다. 실제 API 요청은0입니다. 분류는 항목당 최초1회와 일시적 실패 추가1회, 우선순위는 bug일 때1회이므로 `transport_attempts_max`는 입력 수의3배입니다. 실제 호출 수는 분류 결과와 실패에 따라 달라집니다.

기존 입력 묶음은 가상10건과 실제5건을 포함합니다. 이 명령은 과거 실행을 재생하는 기능이 아니라 **선택한 입력을 새로 분류하는 흐름**입니다. 기존 실험을 다시 실행할 의무는 없습니다. `--smoke`는 묶음의 첫 입력1건만 선택하며 bug이면 두 단계가 실행됩니다. 분류 재시도까지 있으면 최대3회입니다.

## 사용자가 새 실행을 선택할 때

```powershell
$output = "data/llm/pipeline/smoke-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
python -B -m triage_agent.triage_pipeline --inputs 'data/llm/priority/inputs.json' --execute --smoke --env-file '.env' --output $output
```

`--execute`와 존재하지 않는 새 `--output`을 함께 지정해야 호출합니다. 출력이 이미 있거나 입력이 잘못됐으면 호출 전에 거부합니다. `GEMINI_API_KEY`는 실행할 때만 읽고 기존 환경변수가 환경파일보다 우선합니다. `--smoke`를 생략하면 묶음 전체를 대상으로 합니다. 같은 입력의 과거 결과를 덮어쓰거나 점수를 합치지 않습니다.

- 분류: 기존 Gemini 설정, 출력1024·timeout45초·보류 허용·일시 오류 추가1회. 동결 v2 SHA `8c5975944151d7d8e6ed0f0b4a92fb715e15f38ffe1f63f2b2760bf0ec90b518`.
- 우선순위: 기존0.3 어댑터, 출력4096·timeout60초·temperature0·재시도0. SHA `dd62f2444751b9b08ac91fbe4d55e9f3dbae555131cb81a3f5b6235650cfa54b`.
- 두 단계와 분류 재시도를 통틀어 요청 시작 간격 기본13초를 적용합니다. `--interval`로0~60초를 명시할 수 있습니다.
- 입력은 기존 `priority-preparation-inputs-v0` 형식이며 각 항목은 case_id/item_id/origin/input/input_sha256만 허용합니다. 모델에는 title/body만 전달하고 앞선 모델의 rationale·정답·원본 라벨은 넘기지 않습니다.
- 두 프롬프트 중 더 긴 것까지 포함해 입력12000자를 검사합니다. 한도를 넘으면 사전에 전체 실행을 거부하며 본문을 자르지 않습니다. 토큰 한도 검증을 대신하는 것은 아닙니다.

## 저장 결과와 연결 규칙

| 위치 | 내용 |
|---|---|
| `results/<case_id>.json` | 공통 식별자·입력 해시·종합 상태·검토 필요 여부·두 단계 결과 |
| `classification/results/<case_id>.json` | 유형 분류 결과·설정·시도·사용량 |
| `priority/results/<case_id>.json` | 우선순위 결과 또는 호출하지 않은 이유 |
| `run-manifest.json` | 단계별 설정·프롬프트 해시·입력 파일 해시·선택 범위 |
| `classification-prompt.txt`, `priority-prompt.txt` | 실제 사용한 프롬프트 |
| `summary.json` | 단계별 실제 요청 수·성공/보류/실패/미실행·중단 위치 |

분류 success·bug·needs_review=false만 우선순위를 호출합니다. non-bug는 not_applicable, 분류 보류는 classification_review, 분류 실행 실패는 upstream_failed로 우선순위에 기록합니다. 우선순위의 정상 보류는 success이고 다음 입력을 계속 처리합니다. `needs_review`는 사람 판단에 필요한 정보가 부족하다는 결과이며 개발 진행의 승인 대기를 뜻하지 않습니다.

어느 단계든 최종 실행 실패가 나오면 종료코드2로 중단합니다. 앞선 분류 성공과 실패 원인은 보존하고 나머지 입력은 unattempted로 저장합니다. 종합 성공 건수에는 정상 비대상도 포함하므로 유형·등급 정확도로 해석하지 않습니다. 실행이 모두 끝난 분류 폴더는 기존 우선순위 CLI의 `--classifications`에도 사용할 수 있습니다. 실패 후 unattempted가 섞인 폴더를 정상 완료 묶음처럼 사용하지 않습니다.

이 명령은 단일 프로세스의 순차 실행입니다. 기존 review_v2 방식과 같이 호출 중 Gemini transport를 일시적으로 감싸며 finally에서 복구합니다. 동시에 여러 스레드에서 이 함수를 실행하는 서비스는 이번 범위가 아닙니다. 파일 시스템 자체의 쓰기 오류에 대한 복구·자동 재개는 지원하지 않습니다.

## 구현 계획과 확인

목표는 기존 두 어댑터를 연결하는 최소 실행 단위입니다. Python 표준 라이브러리와 기존 입력 검증·결과 저장·응답 검증을 재사용합니다. 별도 분류 결과를 수동으로 복사하는 대신 한 입력의 두 단계를 순서대로 호출하고 즉시 기록하는 구성을 선택했습니다.

- [x] 두 단계 분기·보류·실패 보존·입력 유출 방지·호출 없는 검증·실제 요청 간격에 대한 테스트 작성. 모듈 미구현으로 실패함을 확인.
- [x] `triage_pipeline.py` 구현, 검증된 기존 v2 원문을 package resource로 추가. 기존 프롬프트·클라이언트는 수정하지 않음.
- [x] HTTP 경계만 대체한 CLI 통합 테스트로 저장 결과와 호출 payload 확인. 실제 모델 요청 없음.

팀 연결 담당자는 종합 결과의 classification/priority와 공통 item_id/input_sha256을 사용하면 됩니다. 중복 탐지·PR 요약·GitHub 자동 게시는 각 담당 기능과 연결할 후속 범위이며 이번 두 단계 완료를 네 기능 전체 통합 완료로 표시하지 않습니다.
