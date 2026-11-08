# 저장된 PR의 요약 실행

`pr_summary`는 `pr-summary-inputs-v0` 입력의 PR을 Gemini로 요약하고 기존 `feature_report`에 연결할 결과를 저장합니다. 기본은 환경파일·API 키·네트워크·파일 생성 없는 입력 검증입니다. Python 3.11 이상에서 프로젝트 루트 기준으로 실행합니다.

```bash
python -m pip install -e .
python -B -m triage_agent.pr_summary --inputs '<준비한 폴더>/prs.json' --case-id P12
```

`--case-id`를 생략하면 전체 입력이 선택됩니다. 입력 파일 전체의 형식과 해시를 확인하고, 선택된 PR만 문안 + JSON 입력의 12,000자 제한을 검사합니다. 초과 항목은 `prompt_input_too_long` 진단으로 호출 전에 거부하며 자동으로 자르거나 제외하지 않습니다. 문자 검사는 토큰 예산 검증을 대신하지 않습니다. 파일 선별·긴 diff 분할은 후속 작업입니다.

## 실제 1건 호출과 보고서

`GEMINI_API_KEY`를 환경변수 또는 `.env`에 준비한 후 실행합니다. PR 요약의 기본 모델은 실제 1건 호출을 확인한 `gemini-3.6-flash`입니다. 다른 모델은 `--model`로 지정합니다. `.env`의 `GEMINI_MODEL`은 이 명령의 모델을 변경하지 않습니다. 사용 가능 여부와 품질은 실제 호출로 별도 확인해야 합니다.

```bash
python -B -m triage_agent.pr_summary --inputs '<준비한 폴더>/prs.json' --case-id P12 --execute --env-file .env --output data/llm/pr-summary-run-01
python -B -m triage_agent.feature_report --pr-inputs '<준비한 폴더>/prs.json' --pr-summaries data/llm/pr-summary-run-01/results --output data/llm/pr-summary-report-01
```

출력 폴더는 새 경로여야 합니다. `results/<case_id>.json`에는 식별자·입력 해시·모델·문안 버전·상태·검증된 prediction을 저장합니다. `run-manifest.json`, `prompt.txt`, `summary.json`도 생성합니다. 결과 폴더의 `results`만 보고서의 `--pr-summaries`에 넘깁니다. 이 실행기는 GitHub 댓글을 게시하지 않습니다.

입력은 number/title/body/files와 파일별 filename/patch/patch_truncated입니다. 출력은 [기존 7필드 계약](feature-report.md#pr-요약-연결)을 따릅니다. patch 누락·잘림은 모델에 전달하며 보고서의 검토 필요 표시에도 보존합니다. 테스트 코드 존재를 실행 성공으로 해석하지 않습니다. 모델이 정보 부족을 제대로 표현했는지 사람의 확인이 필요합니다.

현재 문안은 두 실행기가 공유하는 `pr-summary-v0.1`입니다. `tests`에는 실제 테스트 코드 변경이나 입력에 명시된 실제 실행 결과만 넣도록 지시합니다. 문서에 추가한 향후 평가 계획·지표·체크리스트는 `key_changes`에서 문서 변경으로 요약하고, 테스트 근거가 없으면 `tests=[]`로 둡니다. `pr-summary-v0.txt`는 아래 변경 전 기록을 재현하기 위한 이전 문안으로 보존합니다. 형식 검증기는 의미의 정확성까지 보장하지 않으므로 내용 검토가 필요합니다.

호출 사이 기본 간격은 13초(`--interval`), 자동 재시도는 0회입니다. 첫 실패에 중단하고 남은 선택 항목은 `unattempted`로 저장합니다. API 오류·잘못된 JSON·출력 형식 오류·불완전 응답은 성공과 구분해 저장하고 종료코드 2를 반환합니다. 모델이 반환한 API 키 문자열은 JSON 해석 후 가립니다. 기본 검증과 모의 응답 테스트 통과는 실제 모델의 요약 품질을 보장하지 않습니다.

## 실제 개발 표본 확인 (2026-11-05)

동일한 공개 PR #12 입력에서 `gemini-3.8-flash`는 503과 timeout으로 실패했습니다. 계정의 모델 목록을 조회해 `gemini-3.6-flash` 사용 가능 여부를 확인한 뒤, 해당 모델로 1회 호출하여 success와 요청/응답 모델 일치를 확인했습니다. 이것만으로 3.8 전체 서비스 장애나 두 모델의 품질 우열을 확정하지 않습니다.

PR #12의 문서 2개 변경에 대한 요약·영향 파일이 입력과 일치했고, 존재하지 않는 테스트 실행 성공을 주장하지 않았습니다. 기존 보고서에 연결한 결과는 PR #12 ready, 실행하지 않은 PR #11 not_ready입니다. 총 2건의 입력 중 1건만 실제 검증했으며 전체 성능 평가가 아닙니다. 이 기본 모델 변경은 PR 요약 실행기에만 적용됩니다.

## Ollama로 로컬 실행

Gemini 실행기와 별도로 `pr_summary_ollama`를 사용합니다. 외부 API 키를 읽지 않고 로컬 `127.0.0.1:11434`만 호출합니다. Google API 사용량은 소모하지 않습니다. Ollama 설치 및 모델 다운로드에는 인터넷이 필요합니다.

macOS에서 Homebrew로 설치하는 경우:

```bash
brew install ollama
OLLAMA_NO_CLOUD=1 OLLAMA_NUM_PARALLEL=1 OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 ollama serve
```

서버 터미널을 열어 둔 채 다른 터미널에서 다운로드합니다. Ollama macOS 앱을 실행한 경우 별도로 `ollama serve`를 시작할 필요는 없습니다. 기본 모델은 약 7.38GB의 SuperGemma4 12B Q4_K_M이며, 프로그램 실행에는 파일 크기 외에도 메모리가 필요합니다.

```bash
ollama pull hf.co/Jiunsong/SuperGemma-4-12b-abliterated-gguf-4bit:Q4_K_M
ollama list
```

프로젝트 루트에서 같은 입력과 문안을 사용합니다. 기본은 호출 없는 검증이고 `--execute`를 명시해야 모델을 실행합니다.

```bash
PYTHONPATH=src python3 -B -m triage_agent.pr_summary_ollama --inputs '<준비한 폴더>/prs.json' --case-id P12
PYTHONPATH=src python3 -B -m triage_agent.pr_summary_ollama --inputs '<준비한 폴더>/prs.json' --case-id P12 --execute --output data/llm/pr-summary-ollama-01
PYTHONPATH=src python3 -B -m triage_agent.feature_report --pr-inputs '<준비한 폴더>/prs.json' --pr-summaries data/llm/pr-summary-ollama-01/results --output data/llm/pr-summary-ollama-report-01
```

기본 설정은 `--context 8192 --max-tokens 2048 --timeout 600`입니다. 앞서 일반 채팅의 시작값으로 안내한 4,096 대신 PR 원문·문안·출력 공간을 고려해 8,192로 지정합니다. 요청은 순차 실행하며 완료 후 모델을 메모리에서 내립니다. 메모리 부족 시 더 작은 모델이나 명시적으로 선택한 짧은 입력으로 별도 실험합니다.

문자 수 제한은 기존과 동일하게 12,000자이며 실제 모델 토큰 수를 보장하지 않습니다. Ollama가 실제로 읽은 `prompt_eval_count`, 생성한 `eval_count`, 지연 시간, 설치 모델의 크기·양자화, 요청 컨텍스트를 결과에 기록합니다. 서버 측 입력 처리까지 검증하기 전에는 긴 PR 전체를 읽었다고 주장하지 않습니다. 클라이언트는 본문·patch를 추가로 자르거나 제외하지 않습니다.

Ollama 모드의 `network_calls`는 로컬 생성 HTTP 요청 수이고 `external_api_calls`는 0입니다. 실행 전 `/api/show` 조회는 생성 횟수에 포함하지 않습니다. 실패 후 자동 재시도하지 않고 남은 항목은 unattempted로 저장합니다. 기존 Gemini 결과와 별도 폴더에 저장해 동일 입력·문안 해시로 비교합니다.

실제 실행에 앞서 Ollama 버전과 `ollama list`의 모델 ID, 칩·메모리 및 설치 모델의 양자화를 기록합니다. 로컬 서버를 계속 실행해야 하며, 팀원의 PC나 GitHub Actions에서는 각 환경에 실행 서버가 필요합니다. 맥의 localhost 주소가 다른 팀원의 서버를 가리키지는 않습니다.

Ollama 실행기의 자동 테스트는 모의 응답 기반이며, 실제 1건 확인은 아래에 별도로 기록합니다. 다른 PR이나 중복 탐지까지 검증한 것으로 해석하지 않습니다.

### 실제 로컬 1건 확인 (2026-11-07)

- 환경: 사용자 확인 M4 Pro·24GB, Ollama 로컬 서버. 설치 모델 ID `fd53a924ba06`, Q4_K_M, 모델 보고 크기 11.9B.
- 입력: 기존 PR #12의 문서 변경 2개. 이 최초 로컬 v0 실행은 Gemini v0 실행과 입력 해시·문안 해시가 같습니다.
- 요청/응답 모델: `hf.co/Jiunsong/SuperGemma-4-12b-abliterated-gguf-4bit:Q4_K_M` 일치.
- 결과: 로컬 생성 1회, 외부 API 0회, status=success, 지연 시간 29.064초(모델 로드 등 포함).
- 토큰: prompt_eval_count=2808, eval_count=317, 요청 컨텍스트 8192. 이 수치를 Gemini 토큰과 동일한 척도로 비교하지 않습니다.
- 보고서: PR #12 ready, 미실행 PR #11 not_ready. 새 실제 출력은 로컬 데이터 폴더에 보존하고 기존 Gemini 출력을 덮어쓰지 않았습니다.
- 내용 검토: 핵심 문서 변경은 입력과 일치합니다. 다만 v0의 tests 목록에 실제 테스트 변경·실행 대신 향후 평가 계획이 포함됐습니다. 성공 주장은 없지만 계획/실행 구분이 불명확해 아래 v0.1 보완으로 이어졌습니다.

### tests 계획/실행 구분 보완 (2026-11-07)

동일한 PR #12와 같은 SuperGemma4 12B Q4_K_M, context=8192, max_tokens=2048, think=false로 문안만 v0에서 v0.1로 바꾸어 1회 재실행했습니다.

| 항목 | v0 | v0.1 |
|---|---|---|
| 입력 SHA-256 | `d7afccc20e044c4bb612a71f3172a9285400e794b281c2a689a87f792b8421b3` | 동일 |
| 문안 SHA-256 | `eab37be330724da1eb219eb4cf820943ba15dd9e41978be0e134d704bd8b0f9a` | `c4b773144998324d72a9dc6d0592809d3ad7ed1a396f1eca4e39dbb446cd822d` |
| tests | 정확도/F1 등 향후 평가 계획 2개 포함 | `[]` |
| 평가 설계 요약 | tests에도 섞임 | key_changes에 문서 변경으로 요약 |
| 실행 상태 | success | success |
| 지연 시간(로드 등 포함) | 29.064초 | 29.789초 |
| 로컬 생성 / 외부 AI API | 1 / 0 | 1 / 0 |
| 입력 / 출력 토큰 | 2808 / 317 | 2946 / 328 |

v0.1 결과는 핵심 문서 변경을 유지했고, 실제 실행하지 않은 테스트 성공을 주장하지 않았습니다. 기존 보고서에 다시 연결해 PR #12 ready, 미실행 PR #11 not_ready를 확인했습니다. 이전 출력은 덮어쓰지 않았습니다. 이 확인은 개발 표본 1건의 문제 재현·수정 검증이며 품질 평가 점수나 속도 우열의 근거가 아닙니다. Gemini의 v0.1 실제 재실행은 하지 않았습니다.

참고: [Ollama 로컬 API](https://docs.ollama.com/api/chat), [Hugging Face GGUF 실행](https://huggingface.co/docs/hub/en/ollama).
