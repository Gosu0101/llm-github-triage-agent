# LLM 기반 GitHub 이슈·PR 자동 트리아지 에이전트

14팀 스텝업 프로젝트 개발 저장소입니다.

## 프로젝트 목표

GitHub에 새 Issue 또는 Pull Request가 등록되면 다음 기능을 수행하는 자동 트리아지 에이전트를 설계·구현하고 공개 오픈소스 데이터로 성능을 검증합니다.

1. 이슈 유형 분류
2. 우선순위 추정
3. 중복 이슈 탐지
4. PR 변경사항 요약

최종 분석 결과는 GitHub Label과 Comment 형태로 게시하는 것을 목표로 합니다.

## 저장소 구분

- 이 저장소: 에이전트 소스코드, 테스트 코드, 기술 문서, 개발용 Issues/PR 관리
- `llm-github-triage-demo`: 자동 트리아지 시연용 public 저장소

개발 저장소의 업무용 Issue와 시연 저장소의 분석 대상 Issue는 분리하여 관리합니다.

## 역할

| 담당 | 주요 역할 |
|---|---|
| 여윤성·팀장 | 벤치마크 저장소 선정·GitHub Issue/PR 데이터 수집, 이슈 유형 분류·우선순위 추정, 전체 일정·Repository 관리 |
| 강세웅 | GitHub OAuth 인증·권한·Token/Secret 관리, LLM 후보 조사, 중복 이슈 탐지·PR 요약 로직 |
| 이형언 | 평가지표 설계·측정, GitHub Actions 워크플로우 및 임베딩 캐싱 설계 |

## 문서 관리

- 협업 규칙: `CONTRIBUTING.md`
- 설계·API·데이터·평가 기술 문서: `docs/`
- 회의록·학습 노트·수행일지·보고서 초안: Notion

## 현재 상태

프로젝트 협업 환경을 초기 구성하는 단계입니다. 구현되지 않은 기능은 완료 기능으로 간주하지 않습니다.

## OAuth 로컬 검증

외부 웹 프레임워크 없이 Python 3.11 이상의 표준 라이브러리로 GitHub OAuth 인증을 확인할 수 있습니다.

```bash
PYTHONPATH=src python3 -m triage_agent.oauth_server
```

실행하면 기본 브라우저에서 OAuth 승인을 시작합니다. 예시 callback은 `http://127.0.0.1:8080/callback`이며, OAuth App 설정과 `.env`의 `GITHUB_REDIRECT_URI`가 정확히 같아야 합니다. 승인 후 Token은 Git에서 제외된 로컬 `.env`에 저장되고, 지정 저장소의 Issue·PR 수집 결과는 `data/oauth/`에 저장됩니다. 자세한 설정·검증 방법은 [`docs/oauth.md`](docs/oauth.md)를 참고합니다.

최초 승인 이후에는 callback 서버 없이 저장된 Token으로 다시 수집할 수 있습니다.

```bash
PYTHONPATH=src python3 -m triage_agent.collect_saved
```

## 보안

토큰, API Key, Client Secret 등 실제 인증정보는 저장소에 커밋하지 않습니다. 로컬에서는 환경변수 또는 `.env`를 사용하고, 공개 가능한 변수 이름만 `.env.example`에 기록합니다.
