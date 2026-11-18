# 시연 저장소에 트리아지 결과 게시

윤성의 분류·우선순위 결과, 중복 판정 결과, 또는 PR 요약을 기존 `feature_report`로 연결해 `report.json`을 만든다. 게시기는 보고서에서 이벤트 대상 한 건만 선택한다. 이슈에는 유형과 우선순위 라벨 및 분석 댓글을, 내부 브랜치 PR에는 요약 댓글을 작성한다. 요약 입력에 들어가지 못한 diff의 파일명도 댓글에 표시한다.

먼저 게시 내용을 미리 확인한다. 아래 42는 형식 예시이므로 시연 대상 번호로 바꾼다. PR 미리보기에는 `--kind pull_request`를 사용한다.

```bash
python -B -m triage_agent.publish --report path/to/report.json --kind issue --number 42
```

시연 저장소에서 만든 보고서에는 `item_id`가 `Gosu0101/llm-github-triage-demo#42` 형식이어야 한다. 실제 게시에서는 같은 명령에 `--publish`를 추가하고 토큰은 환경변수 `GITHUB_TOKEN`으로 전달한다.

```bash
python -B -m triage_agent.publish --report path/to/report.json --kind issue --number 42 --publish
python -B -m triage_agent.publish --report path/to/report.json --kind pull_request --number 42 --pr-head-sha "$PR_HEAD_SHA" --publish
```

- 형언 인계. 시연 저장소의 Actions 토큰에는 이슈 라벨·댓글을 위해 `issues: write`, PR 댓글을 위해 `pull-requests: write`가 필요하다. 같은 작업에서 코드 체크아웃을 사용하면 `contents: read`도 설정한다. `GITHUB_TOKEN`은 워크플로 토큰을 환경변수로 전달한다. 로컬 토큰은 같은 저장소에 대한 Issues 및 Pull requests 쓰기 권한을 부여한 계정을 사용한다.
- 이벤트에서 가져온 이슈·PR 번호를 `--number`에 넘긴다. 이슈의 현재 제목·본문이 보고서 작성 시점과 다르면 보고서를 새로 만든다. PR은 분석에 사용한 head commit SHA를 `--pr-head-sha`에 넘긴다. Actions에서는 `github.event.pull_request.head.sha`를 사용한다. 분석 이후 PR 브랜치에 커밋이 추가됐다면 게시기를 실행할 때 감지해 게시를 중단한다.
- 게시기는 동일 작성자의 식별 표식이 붙은 댓글 하나를 다시 사용한다. 내용이 같으면 그대로 두고 달라졌으면 해당 댓글을 수정한다. 첫 게시가 동시에 진행되지 않도록 형언의 워크플로에서 이슈·PR 번호별 동시 실행 그룹을 설정한다.
- 에이전트 라벨은 `triage:type:*`, `triage:priority:*` 이름으로 생성한다. 성공한 판정으로 해당 그룹을 갱신한다. 보류되거나 적용 대상이 아니면 해당 그룹의 에이전트 라벨을 걷어낸다. 누락·실패한 단계는 기존 라벨을 유지하며 최신 상태는 댓글에서 확인한다.
- 게시 성공 시 JSON의 `comment_url`과 `comment_action`을 실행 결과로 보관한다. 통합 시연에서는 동일 보고서로 두 번 실행해 첫 댓글 URL과 재실행 URL이 같고 두 번째 `comment_action`이 `unchanged`인지 확인한다. 모의 결과는 미리보기에서 확인한다.

[GitHub 라벨 API](https://docs.github.com/en/rest/issues/labels) · [GitHub 이슈 및 PR 댓글 API](https://docs.github.com/en/rest/issues/comments)
