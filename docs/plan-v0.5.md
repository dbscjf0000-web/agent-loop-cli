# agent-loop-cli v0.5.0 — MCP server 계획

## 1. 목표 (한 문장)

v0.4.x 까지 사람이 직접 쓰던 `agent-loop` CLI 를, **다른 AI 에이전트**(Claude
Code / Cursor / OpenCode / 임의의 MCP 클라이언트)가 표준 **Model Context
Protocol** 로 호출할 수 있게 한다. 현재 사용자(본인)는 CLI 를 그대로 쓰고,
"남이 쓰기 편한 인터페이스"가 한 층 더 생긴다.

## 2. 비기능 요구사항 (불변 원칙)

| 원칙 | 의미 |
|---|---|
| **No new deps** | `mcp` SDK 도입 X. JSON-RPC 2.0 은 stdlib `json` + stdin/stdout 으로 직접 구현. |
| **Backward compat** | v0.4.2 의 모든 CLI / config / artifact 스키마 그대로. MCP 는 신규 진입로일 뿐. |
| **Privacy first** | 다른 task 의 task.md / 코드 / prompt 본문은 절대 노출 X. `cross_task=False` 면 `agent-loop://global/*` 도 거부. |
| **Stateless** | server 재시작해도 task 상태 무사 — 모든 상태는 여전히 `~/.agent-loop/` + `<root>/<task_id>/` 파일. server 메모리에 캐시 X. |
| **Fail-soft** | client 가 잘못된 JSON / 잘못된 tool 이름을 보내도 server 는 죽지 않고 표준 JSON-RPC error 반환. |
| **CLI parity** | MCP tool 시맨틱 = 같은 이름 CLI 명령. 새 동작 만들지 않음 (tool = thin wrapper). |

## 3. 표준 — Model Context Protocol (간단 소개)

- **Anthropic 발표** (2024-11), 현재 Claude Desktop / Claude Code / Cursor /
  OpenCode 등 주요 AI 클라이언트가 채택.
- **JSON-RPC 2.0** 메시지를 transport (stdio 또는 HTTP) 로 주고받는다.
- **server 가 노출하는 3 가지** :
  - **Tools** — client 가 호출 가능한 함수. 입력 스키마 정의.
  - **Resources** — client 가 read 가능한 컨텐츠. URI 로 식별.
  - **Prompts** — 미리 정의된 프롬프트 템플릿 (v0.5.0 에서는 노출 X).
- **client 가 부르는 메서드** (이번에 구현 대상):
  - `initialize` — 핸드셰이크 (protocol version, capabilities).
  - `tools/list` — tools 목록.
  - `tools/call` — 특정 tool 실행.
  - `resources/list` — resources 목록.
  - `resources/read` — URI 컨텐츠 read.

## 4. Transport

| 옵션 | v0.5.0 | 비고 |
|---|---|---|
| **stdio** | YES (default) | client 가 `python3 -m agent_loop.cli mcp serve` 를 spawn 후 stdin/stdout 으로 JSON-RPC. 가장 단순. Claude Code `.mcp.json` 의 표준 패턴. |
| **HTTP** | 후보 (v0.5.x) | 원격/공유 server 시. `--transport http --port N` 로 후속에 추가. SSE / WebSocket 별도 검토. |

KISS 원칙으로 stdio 만 우선. 코드 구조는 transport 추상화는 하되 (`serve_stdio`),
`serve_http` 는 placeholder 둠.

## 5. 노출되는 Tools (6 개)

```jsonc
[
  {"name": "agent_loop.run",
   "description": "Drive a task through R->P->I->V->J cycles.",
   "inputSchema": {
     "type": "object",
     "required": ["task"],
     "properties": {
       "task":       {"type": "string"},
       "cycles":     {"type": "integer", "minimum": 1, "default": 5},
       "mode":       {"type": "string",  "enum": ["auto","supervised"], "default": "auto"},
       "max_redo":   {"type": "integer", "minimum": 1, "default": 3},
       "cross_task": {"type": "boolean", "default": true}
     }
   }},

  {"name": "agent_loop.list",
   "description": "List task directories under root.",
   "inputSchema": {
     "type": "object",
     "properties": {
       "root": {"type": "string", "description": "Override the state root."}
     }
   }},

  {"name": "agent_loop.status",
   "description": "Return cycle / phase / latest score for a task.",
   "inputSchema": {
     "type": "object",
     "required": ["task_id"],
     "properties": {"task_id": {"type": "string"}, "root": {"type": "string"}}
   }},

  {"name": "agent_loop.resume",
   "description": "Continue a paused task from its last checkpoint.",
   "inputSchema": {
     "type": "object",
     "required": ["task_id"],
     "properties": {"task_id": {"type": "string"}, "root": {"type": "string"}}
   }},

  {"name": "agent_loop.bench",
   "description": "Run a benchmark from benchmarks/.",
   "inputSchema": {
     "type": "object",
     "required": ["name"],
     "properties": {"name": {"type": "string"}, "cycles": {"type": "integer"}}
   }},

  {"name": "agent_loop.memory_show",
   "description": "Read the latest N lines of the cross-task patterns.md.",
   "inputSchema": {
     "type": "object",
     "properties": {"limit": {"type": "integer", "minimum": 1, "default": 50}}
   }}
]
```

각 tool 은 `{"content": [{"type": "text", "text": "..."}]}` 형식의 표준 MCP
응답을 돌려준다. `agent_loop.run` 의 동기 실행 결과는 `RunResult.as_dict()`
JSON 을 text 로 직렬화. status / list / memory_show 는 표 / 라인 형태 텍스트.

## 6. 노출되는 Resources (4 종)

```
agent-loop://task/{id}/solution    → workspace/best_solution.py | solution.py
agent-loop://task/{id}/memory      → memory/episodic.md + memory/core_facts.md (concat)
agent-loop://task/{id}/metrics     → telemetry/metrics.jsonl
agent-loop://global/patterns       → ~/.agent-loop/global/patterns.md
```

`resources/list` 는 위 4 종을 항상 노출하지만 (URI 패턴 정보), `resources/read`
는 다음 규칙을 따른다:

- **task scope**: 요청된 task_id 의 파일만 읽음. 다른 task 는 별도 read 호출
  필요. `task.md` / `prompt` 자체는 노출 X (privacy: 사용자 raw 입력 보호).
- **global scope**: `cross_task_memory=False` 일 때 `agent-loop://global/*`
  read 는 `ERR_PRIVACY_DISABLED (-32000)` 반환.
- **존재 확인**: 파일이 없으면 `ERR_INVALID_PARAMS` + `"resource not found"`.

## 7. Privacy / 보안 원칙

| 항목 | 처리 |
|---|---|
| 다른 task 의 task.md / prompt | 노출 X (resources 에 URI 자체가 없음) |
| 본 task 의 코드 (`solution.py`) | 노출 OK (해당 task 자체 결과물) |
| 본 task 의 memory (episodic + core_facts) | 노출 OK |
| 글로벌 patterns.md | `cross_task=True` 일 때만 |
| `task_index.jsonl` (다른 task 요약 모음) | 노출 X — 다른 task ID 가 누설되어 enumeration 공격 가능. v0.5.0 에서는 `agent_loop.list` tool 로만 노출하되 같은 보안 검토 적용 (현 사용자 단일 호스트 가정이라 OK). |
| LLM API 키 / cli credential | 노출 X (handler 가 절대 dump 하지 않음) |
| `agent_loop.run` 이 받은 task 텍스트 | 그대로 그 task 의 task.md 에만 저장. server 메모리 / 다른 task 에 누설 X. |

## 8. 의존성 결정 — 자체 구현 vs `mcp` SDK

| 옵션 | 장 | 단 | 결정 |
|---|---|---|---|
| `mcp` 공식 SDK (`pip install mcp`) | 표준 준수 자동, 향후 기능 (resources/templates, prompts) 자동 | 새 의존성 1 개. 패키지 deps 늘어남 | NO |
| **stdlib + 자체 50 줄** | 의존성 0, 표면 작음, 디버깅 쉬움 | 표준 변화 시 수동 추적 | **YES** |

JSON-RPC 2.0 + 단일 transport 는 50~100 줄로 충분히 구현 가능. v0.5.0 의
6 tools / 4 resources 는 SDK 없이도 안전. 향후 prompts / sampling 등 더
복잡한 MCP feature 가 필요해지면 v0.6+ 에 SDK 도입 검토.

## 9. 모듈 구조

```
src/agent_loop/
└── mcp/
    ├── __init__.py        # protocol version, server name 상수 (~10줄)
    ├── protocol.py        # JSON-RPC 2.0 요청/응답 dataclass + 파서 (~80줄)
    ├── handlers.py        # tools/resources 디스패처 (~280줄)
    └── server.py          # stdio loop + transport stub (~120줄)
```

각 파일 책임:

- `protocol.py` — `Request`, `Response`, `parse_request`, `serialize_response`,
  `make_error`, MCP error code 상수. transport-agnostic.
- `handlers.py` — `Handlers(config, root)` 클래스. 매 method 핸들러 + tool 별
  private 함수. **stateless**: 매 호출마다 ContextEngine / TaskDir / Orchestrator
  새로 생성.
- `server.py` — `serve_stdio(config, root)` 가 `for line in sys.stdin` 루프 +
  dispatch. EOF (stdin 닫힘) 시 정상 종료.

## 10. CLI 통합

```python
mcp_app = typer.Typer(help="(v0.5) Model Context Protocol server interface.")

@mcp_app.command("serve")
def mcp_serve(
    transport: str = typer.Option("stdio", "--transport"),
    config_path: Optional[Path] = typer.Option(None, "--config"),
    root: Path = typer.Option(Path("./.agent_loop"), "--root"),
) -> None: ...

@mcp_app.command("tools")
def mcp_tools() -> None: ...

@mcp_app.command("resources")
def mcp_resources() -> None: ...

app.add_typer(mcp_app, name="mcp")
```

`serve` 만 server 진입점. `tools` / `resources` 는 introspection (운영자가
client 설정 짜기 전 미리 노출되는 인터페이스 확인용 — Rich 표).

## 11. 동시성 / long-running 처리

### 11.1 server 자체

- MCP server 는 **long-running process** (CLI 의 한 호출 = 한 명령 원칙과 다름).
- 그러나 **상태는 여전히 파일** — server 가 죽어도 task 진행은 디스크에 남음.
- ContextEngine, TaskDir, Orchestrator 등은 매 tool call 마다 **새로 생성**.
  CLI 와 동일한 stateless 패턴 유지.

### 11.2 `agent_loop.run` 동작 정책

| 정책 | 장 | 단 | v0.5.0 |
|---|---|---|---|
| **Sync (blocking)** | 단순. client 가 완료까지 기다리면 RunResult 반환 | client timeout 길어야 함 (수 분~수 시간) | **YES** |
| Async (background thread) | client 즉시 task_id 받고 polling | thread / lock 복잡, status race | NO (v0.5.x 후보) |

v0.5.0 은 sync. Claude Code / Cursor 의 MCP 호출은 long timeout 이 default
(`.mcp.json` 에 `timeoutMs` 없음 = 무한). 사용자가 타임아웃 짧으면 `--cycles 1` 추천.

### 11.3 동시 task 실행

- v0.5.0: **단일 server 내 sequential** — 두 client 가 동시에 `agent_loop.run`
  호출하면 첫 번째가 끝날 때까지 두 번째 차단 (MCP server 가 single-thread
  loop). global memory race condition 회피용 KISS.
- v0.5.x: thread pool + per-task lock 검토.
- v1.0+: 본격적 race fix (`fcntl.flock` on patterns.md).

## 12. 작업 분해

| # | 작업 | 산출물 |
|---|---|---|
| 1 | `docs/plan-v0.5.md` (이 파일) | this |
| 2 | `src/agent_loop/mcp/__init__.py` | 상수 (~10 lines) |
| 3 | `src/agent_loop/mcp/protocol.py` | JSON-RPC dataclass + 파서 (~80 lines) |
| 4 | `src/agent_loop/mcp/handlers.py` | 6 tools + 4 resources 디스패처 (~280 lines) |
| 5 | `src/agent_loop/mcp/server.py` | stdio loop (~120 lines) |
| 6 | `src/agent_loop/cli.py` 확장 | `mcp serve` / `mcp tools` / `mcp resources` |
| 7 | `tests/test_mcp_protocol.py` | 4 tests (parse / serialize / id / error) |
| 8 | `tests/test_mcp_handlers.py` | 7 tests (initialize / tools list / call / resources) |
| 9 | `tests/test_mcp_cli.py` | 2 tests (serve startup, tools introspection) |
| 10 | `README.md` MCP server 섹션 | doc + `.mcp.json` 예시 |
| 11 | `docs/architecture.md` MCP 박스 | doc |
| 12 | `progress.txt` + `__version__ = "0.5.0"` | release log |

## 13. 성공 기준

1. `pytest -q` : v0.4.2 의 172 + 신규 13 (≥) 모두 green (regression 0).
2. mock e2e: stdin 에 `initialize` + `tools/list` + `tools/call agent_loop.list`
   3 개 라인 주면 stdout 으로 정확한 JSON-RPC 응답 3 개.
3. `cross_task_memory=False` 일 때 `resources/read agent-loop://global/patterns`
   는 `ERR_PRIVACY_DISABLED` 반환.
4. `agent-loop mcp tools` 가 6 개 tool 을 Rich 표로 출력. exit 0.
5. `agent-loop mcp resources` 가 4 개 URI 패턴을 Rich 표로 출력. exit 0.
6. `agent-loop mcp serve` 가 stdin EOF 시 정상 종료 (exit 0).

## 14. 위험 / 한계

| 위험 | 대응 |
|---|---|
| 표준이 바뀌면 자체 구현 broken | MCP spec 의 init / tools / resources 는 안정. 큰 변경 시 v0.6+ 에 `mcp` SDK 도입. |
| client 가 잘못된 JSON 보내면 server crash | parse 실패 시 `ERR_PARSE` 반환, 다음 line 으로 계속. server 는 죽지 않음. |
| sync 실행이 client timeout | `agent_loop.run` 은 sync — client (Claude Code) 의 timeout 이 길어야 함. README 에 명시. |
| 동시 호출 race | single-thread loop 로 sequential 보장 (v0.5.0). |
| 다른 task 노출 누설 | resources URI scheme 이 task scope 강제 (`task/{id}/...`). list 도 task ID 만 (코드/내용 X). |
| privacy disabled 인데 client 가 global URI 반복 시도 | error 만 반환, leakage 0. server log 에는 기록 (v0.5.x). |

## 15. 결정 사항 (확정)

- **stdio only** (v0.5.0). HTTP 는 v0.5.x 후보.
- **자체 구현** (의존성 0). `mcp` SDK 는 v0.6+.
- **sync `run`** — client timeout 이 길어야 함 (Claude Code 는 무한이 default).
- **6 tools / 4 resources** — 표 (§5, §6). 새 동작 만들지 않음 (CLI parity).
- **task.md 본문 / 다른 task 노출 X** — privacy 보존.
- **`cross_task=False` → global 거부** — v0.4 와 일관.
- **server 자체는 long-running 이지만 상태는 파일** — stateless 원칙 유지.
- **모든 핸들러는 `try/except` 로 격리** — 한 tool 실패가 server 를 죽이지 않게.

## 16. 다음 단계 (post-v0.5.0)

1. (v0.5.x) HTTP transport (`--transport http --port N`).
2. (v0.5.x) `agent_loop.run` async + `tools/call agent_loop.status` 로 polling.
3. (v0.5.x) prompts/* 노출 (`prompts/list` / `prompts/get`).
4. (v0.6) `mcp` SDK 도입 — sampling / resource templates 로 client→server LLM
   호출 가능 (server 가 client 의 LLM 사용).
5. (v1.0) multi-process 동시 task + race-safe 글로벌 memory append (`fcntl.flock`).
