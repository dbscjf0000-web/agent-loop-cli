# agent-loop-cli v0.1.0 MVP 계획

## 1. 목표 (한 문장)

`agent-loop-plugin`(Claude Code 전용)을 **standalone CLI + 멀티모델**로 재구성한
첫 동작 가능한 MVP를 만든다.

## 2. 배경

- 기존: `agent-loop-plugin` (Claude Code Skill, Claude only, single phase model)
- 현재: PIAMDA v16 (bash 기반, KISTI 종속)
- 신규 v0.1: **CLI(Python) + litellm 멀티모델 + 5단계 사이클 + 퇴행 방지** 통합

## 3. 비기능 요구사항 (불변 원칙)

| 원칙 | 의미 |
|---|---|
| **Stateless workers** | 각 phase worker는 fresh context. 메모리 공유 없음. |
| **File-based state** | 모든 상태는 `.agent_loop/` 안 파일. 메모리 상 stateful 객체 없음. |
| **CLI-first** | 라이브러리 import 없이 명령어로 사용. `pip install`로 끝. |
| **Multi-model** | Phase별 다른 vendor/모델 지정 가능. |
| **Resumable** | 끊겨도 checkpoint에서 이어감. |
| **Sandbox-safe** | Phase I는 `.agent_loop/workspace/` 안에서만 동작 (default). |

## 4. v0.1 범위 (스코프)

### 포함 ✅
- R → P → I → V → J 5단계 사이클 (plugin과 동일 시맨틱)
- litellm 기반 멀티모델 (Anthropic / OpenAI / Gemini)
- Phase별 모델 매핑 (`config.toml`)
- 퇴행 방지 (`best_solution.json` 롤백)
- Memory 누적 (`memory.txt` — plugin과 동일, 단순 구조)
- `agent-loop run/list/resume/config` 명령어
- Sandbox workspace
- 기본 metrics (`metrics.jsonl`: phase / tokens / latency / cost)
- **4개 reference benchmarks**: `n_queens` (알고리즘), `binary_search` (검색),
  `sort_tuning` (정렬), `palindrome` (문자열) — 각각 `benchmarks/*.yaml`

### 제외 ❌ (이후 버전)
- 3-tier memory + Sensors (v0.2)
- 다축 Verify rubric (v0.2)
- Multi-judge consensus (v0.3)
- Multi-strategy parallel plan (v0.3)
- Cross-task memory (v0.4)
- MCP server mode (v0.4)

## 5. 아키텍처 (4 레이어)

```
CLI (typer)
   │
   ▼
Orchestrator (R→P→I→V→J 루프 + 롤백 + checkpoint)
   │
   ▼
Phase Workers (R/P/I/V/J 각각 함수 + Model Router)
   │
   ▼
State Store (.agent_loop/ 파일)
```

## 6. 모듈 구조

```
agent-loop-cli/
├── pyproject.toml          # 패키지 메타 + 의존성
├── README.md
├── LICENSE                 # MIT (plugin과 동일)
├── docs/
│   ├── plan-v0.1.md        # 이 파일
│   └── architecture.md     # 큰 그림 (이 파일에서 추출 예정)
├── src/agent_loop/
│   ├── __init__.py
│   ├── cli.py              # typer CLI 진입점
│   ├── orchestrator.py     # 사이클 루프 + 롤백
│   ├── workers.py          # R/P/I/V/J 5개 함수
│   ├── models.py           # litellm wrapper
│   ├── config.py           # toml 로딩
│   ├── state.py            # .agent_loop/ 파일 IO
│   └── prompts/
│       ├── research.md
│       ├── plan.md
│       ├── implement.md
│       ├── verify.md
│       └── judge.md
├── benchmarks/
│   └── n_queens.yaml       # 1개 ref task
└── tests/
    ├── test_state.py
    └── test_models.py
```

## 7. 핵심 인터페이스

### CLI

```bash
agent-loop run "<task>" [--cycles N] [--mode auto|supervised] [--max-redo N]
agent-loop list
agent-loop resume <task-id>
agent-loop config init|edit|show
agent-loop --version
```

### Config (`~/.agent-loop/config.toml`)

```toml
[models]
research  = "anthropic/claude-opus-4-7"
plan      = "anthropic/claude-opus-4-7"
implement = "anthropic/claude-sonnet-4-6"
verify    = "anthropic/claude-haiku-4-5"
judge     = "openai/gpt-5.2"

[budget]
daily_usd = 10
per_run_usd = 2

[runtime]
sandbox = true
max_cycles = 10
max_redo = 3
```

### State 디렉토리

```
.agent_loop/
├── task.md
├── memory.txt
├── workspace/              # Phase I 작업 공간 (sandbox)
├── checkpoints/
│   └── cycle_N_phase_X.json
├── artifacts/
│   ├── findings.md         # R 산출물
│   ├── plan.md             # P 산출물
│   ├── execution_log.md    # I 산출물
│   ├── solution.json       # V 산출물
│   ├── best_solution.json  # 최고 기록
│   └── judge_result.json   # J 산출물
└── telemetry/
    └── metrics.jsonl
```

### Phase 간 계약 (Worker → Worker)

| Phase | 입력 | 출력 |
|---|---|---|
| R | task.md, memory.txt | artifacts/findings.md |
| P | task.md, memory.txt, findings.md | artifacts/plan.md |
| I | plan.md, workspace/ | artifacts/execution_log.md, workspace/* |
| V | execution_log.md, workspace/ | artifacts/solution.json |
| J | solution.json, best_solution.json, memory.txt | artifacts/judge_result.json |

### Judge 결정 스키마

```json
{
  "better": true | false,
  "action": "stop" | "redo_R" | "redo_P",
  "reason": "...",
  "hint": "...",
  "scores": { "metric_name": 0.0..1.0 }
}
```

## 8. 의존성

| 라이브러리 | 용도 | 필수 |
|---|---|---|
| `typer` | CLI | ✅ |
| `litellm` | 멀티모델 | ✅ |
| `tomli` (Py<3.11) | toml 파싱 | ✅ |
| `pydantic` | 스키마 검증 | ✅ |
| `rich` | 터미널 출력 (진행 상황) | ✅ |
| `pytest` | 테스트 | dev |

## 9. 작업 분해 (구현 순서)

| # | 작업 | 산출물 | 예상 시간 |
|---|---|---|---|
| 1 | 패키지 골격 + pyproject.toml | 설치 가능 상태 | 30분 |
| 2 | config.py + state.py | 단위 테스트 통과 | 1h |
| 3 | models.py (litellm wrapper) | 1회 호출 성공 | 30분 |
| 4 | prompts/*.md (5개) | 기본 프롬프트 | 1h |
| 5 | workers.py (R/P/I/V/J 함수) | 각 phase 단독 호출 가능 | 2h |
| 6 | orchestrator.py | 1 cycle 끝까지 | 2h |
| 7 | cli.py (run 명령) | `agent-loop run` 동작 | 1h |
| 8 | rollback + checkpoint + resume | 끊겼다가 이어가기 | 1h |
| 9 | benchmarks/*.yaml 4개 + `agent-loop bench` 명령 | binary_search + 1개 이상 통과 | 1.5h |
| 10 | README + 사용 예시 | 다른 사람도 설치 가능 | 30분 |

**총 예상: ~10시간** (한 워커 turn 한도 고려해 여러 워커로 분할 spawn).

## 10. 성공 기준 (v0.1 done)

1. `pip install -e .` 후 `agent-loop --version` 동작
2. `agent-loop config init`으로 config 생성
3. `agent-loop run "Pure Python N-Queens N=8~13, N=13 ≤1.5s"` 실행 시
   - 5단계 사이클 진행 출력
   - `.agent_loop/` 안 모든 파일 생성
   - 2~5 cycle 안에 목표 달성
4. `agent-loop resume <id>`로 끊긴 task 이어가기
5. README의 설치/사용 예시대로 다른 환경에서도 동작
6. 최소 1개 cross-vendor 조합 동작 (예: Claude R/P/I + GPT J)

## 11. 위험 요소 & 대응

| 위험 | 영향 | 대응 |
|---|---|---|
| litellm provider별 인터페이스 차이 | 단일 모델만 동작 | abstraction 한 겹 더 두고, 처음부터 2 vendor 테스트 |
| KISTI 환경 OpenAI/Gemini API 차단 | 멀티벤더 검증 불가 | CCB proxy 활용 (사용자 메모리 참조) |
| sandbox에서 코드 실행 (subprocess) 보안 | 의도치 않은 시스템 영향 | timeout + resource limit + workspace 격리 |
| LLM 비결정성으로 cycle 재현 불가 | 디버깅 어려움 | temperature=0, seed 옵션 |
| 패키지 이름 충돌 (PyPI) | 배포 불가 | `agent-loop-cli` 또는 `agentloop`로 PyPI 검색 후 결정 |

## 12. 결정 사항 (2026-04-27 확정)

- ✅ PyPI 패키지명: **`agent-loop-cli`**
- ✅ Python 최소 버전: **3.10**
- ✅ 벤치 task: **4개** (n_queens, binary_search, sort_tuning, palindrome)
- ✅ Config 위치: **`~/.agent-loop/`** default, 프로젝트 `./agent-loop.toml` override
- ✅ rich 풀활용: 색상 + 이모지(✓ ✗ ⚙) + live status
- ✅ 기존 plugin: 유지 + 양쪽 README에 cross-link, migration은 README 한 섹션

## 13. 다음 단계 (이 plan 승인 후)

1. ✅ 사용자가 12절 Open Questions 답변
2. 워커 spawn → 작업 #1~3 (골격 + config + state + models) 동시 진행
3. 워커 spawn → 작업 #4~7 (prompts + workers + orchestrator + CLI) 순차
4. 워커 spawn → 작업 #8~10 (resume + bench + README)
5. 부모(나)가 각 워커 결과 검토하고 progress.txt 갱신
