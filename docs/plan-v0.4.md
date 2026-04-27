# agent-loop-cli v0.4.0 — Cross-task Memory 계획

## 1. 목표 (한 문장)

v0.3 까지 단일 task 디렉토리 안에서만 누적되던 학습(`memory/core_facts.md`)을 **사용자
홈 글로벌 영역으로 끌어올려 모든 task 가 공통 패턴을 이어받게** 한다. v0.4.1 의 MCP
server 가 외부에서 글로벌 메모리를 노출할 발판이 되며, v0.3 backward compat 은 한 줄도
깨지지 않는다.

## 2. 비기능 요구사항 (불변 원칙)

| 원칙 | 의미 |
|---|---|
| **Backward compat** | `runtime.cross_task_memory=False` 면 v0.3 동작 100% 동일. 글로벌 디렉토리 부재 시도 정상 동작. |
| **Privacy first** | 코드 / prompt 본문은 글로벌에 저장 X. `CORE:` 라인과 한 줄 task 요약만. 사용자가 `memory wipe` 로 즉시 삭제 가능. |
| **No new deps** | stdlib + 기존 typer / rich / pydantic 만. 파일 잠금은 best-effort (`os.O_EXCL` append). |
| **사용자 홈 외부 X** | 데이터는 `~/.agent-loop/global/` 하나만. config 로 override 가능하지만 default 는 홈. |
| **Idempotent** | `commit_to_global` 가 같은 task 두 번 호출되어도 dedup 됨. 글로벌 dir 자동 생성 (race-safe). |
| **Stateless** | ContextEngine 은 인스턴스 캐시 X. snapshot 호출마다 글로벌 파일 fresh read. |

## 3. 범위 (스코프)

### 포함 (이 PR — v0.4.0)
- `~/.agent-loop/global/patterns.md` + `task_index.jsonl` 글로벌 디렉토리.
- `ContextEngine` 확장: `global_root`, `cross_task` 인자 + `commit_to_global()` + `_load_global_patterns()`.
- `MemorySnapshot.global_patterns` 필드 + worker prompt 의 `{memory}` 슬롯에 합쳐 노출.
- `Runtime.cross_task_memory` (bool) + `cross_task_memory_dir` (str) + `cross_task_memory_max_chars` (int) config.
- ENV: `AGENT_LOOP_RUNTIME_CROSS_TASK_MEMORY`, `..._DIR`, `..._MAX_CHARS`.
- `Orchestrator.__init__` 에서 ContextEngine 에 global_root / cross_task 전달, `run()` 마지막에 `commit_to_global()` 호출.
- CLI 신규 subcommand `agent-loop memory {show, wipe, list, path}`.
- 기존 `run` / `bench` 에 `--no-cross-task` flag (한 번 실행만 끄기).
- 단위 테스트 (`tests/test_cross_task.py` 10) + CLI 테스트 (`tests/test_memory_cli.py` 5) + e2e mock (`tests/test_cross_task_e2e.py` 2).
- `README.md` "Cross-task memory (v0.4)" 섹션 + `docs/architecture.md` global memory 박스.
- `progress.txt` 갱신, `__version__ = "0.4.0"`.

### 제외 (다음 워커 / 향후)
- `src/agent_loop/mcp_server.py` — v0.4.1 워커가 본구현. 이 PR 은 hooks 만 남긴다 (commit_to_global / load_global_patterns 가 그 server 의 데이터 소스).
- 글로벌 메모리 기반 retrieval (semantic search, embedding) — v0.5+.
- 글로벌 메모리 에 score / 통계 분석 dashboard — v0.5+.
- 사용자 별 cloud sync (단일 호스트 가정 유지).

## 4. 글로벌 디렉토리 layout

```
~/.agent-loop/global/
├── patterns.md         # CORE: 라인 누적 (영속, dedup).
└── task_index.jsonl    # 과거 task 한 줄 요약 (audit + 향후 retrieval).
```

`patterns.md` 형식:
```
CORE: always include empty-list edge case
CORE: prefer iterative algorithm for N>20
CORE: claude --print + --allowedTools=NoneSuch to block self-invoke
```
- `task_dir/memory/core_facts.md` 의 `CORE:` 시작 라인만 추출.
- 동일 라인 dedup (정규화 X — exact match).
- 라인 단위 append-only (`O_APPEND` write); 절대 truncate X.

`task_index.jsonl` 형식 (라인 = task 1 개):
```json
{"task_id": "abc123", "weighted_score": 0.97, "cycles": 2, "task_md_first_line": "Implement add(a,b).", "timestamp": 1761610000.5, "final_status": "stop"}
```
- `agent-loop memory list` 가 표로 출력.
- `task_md_first_line` 만 저장 (task 본문 / 코드 / prompt 노출 X — privacy).

## 5. ContextEngine 확장

### 5.1 신규 시그니처
```python
class MemorySnapshot:
    episodic: str
    core_facts: str
    history_count: int
    global_patterns: str = ""    # NEW

    def render(self) -> str:
        ep = self.episodic.strip() or "(none)"
        cf = self.core_facts.strip() or "(none)"
        gp = self.global_patterns.strip()
        out = f"# Episodic\n{ep}\n\n# Core Facts\n{cf}"
        if gp:
            out += f"\n\n# Global Patterns (cross-task)\n{gp}"
        return out

class ContextEngine:
    def __init__(
        self,
        task_dir: TaskDir,
        *,
        global_root: Path | None = None,
        cross_task: bool = True,
        global_max_chars: int = 4000,
    ) -> None: ...
```

### 5.2 신규 메서드
- `_global_dir() -> Path` — `~/.agent-loop/global/` (또는 override). `expanduser` + 자동 생성.
- `_global_patterns_path() -> Path` — `<global_dir>/patterns.md`.
- `_global_index_path() -> Path` — `<global_dir>/task_index.jsonl`.
- `_load_global_patterns(max_chars: int | None = None) -> str` — 파일 read, `max_chars` 초과시 끝(최근)에서 잘라냄. cross_task=False 또는 파일 없음 → `""`.
- `commit_to_global(summary: dict[str, Any]) -> dict[str, Any]` — 호출 결과 stat dict 반환.
  - cross_task=False 면 즉시 `{"committed": False, "reason": "disabled"}`.
  - `_global_dir()` 자동 생성 (race-safe `mkdir(parents=True, exist_ok=True)`).
  - `core_facts.md` read → `CORE:` 시작 라인만 골라냄.
  - `patterns.md` read → 기존 라인 set.
  - dedup 후 새 라인만 `O_APPEND` write.
  - `task_index.jsonl` 에 summary 한 줄 append (idempotent: 같은 task_id 가 이미 있으면 skip).

### 5.3 snapshot 변경
- 기존 episodic + core_facts 그대로 + cross_task=True 면 `_load_global_patterns()` 호출.

## 6. Config / CLI 변경

### 6.1 config.py
```python
class Runtime(BaseModel):
    ...
    # v0.4 cross-task memory
    cross_task_memory: bool = True
    cross_task_memory_dir: str = "~/.agent-loop/global"
    cross_task_memory_max_chars: int = 4000
```

`_ENV_MAP` 에 3 줄 추가:
- `AGENT_LOOP_RUNTIME_CROSS_TASK_MEMORY` (bool)
- `AGENT_LOOP_RUNTIME_CROSS_TASK_MEMORY_DIR` (str)
- `AGENT_LOOP_RUNTIME_CROSS_TASK_MEMORY_MAX_CHARS` (int)

`_DEFAULT_TOML` 에 `[runtime]` 섹션 안에 주석 + 키 노출.

### 6.2 cli.py
신규 subcommand 그룹 `memory`:
- `agent-loop memory show [--limit N=50]` — patterns.md 의 마지막 N 라인 출력.
- `agent-loop memory wipe` — typer.confirm 후 디렉토리 삭제.
- `agent-loop memory list` — task_index.jsonl 을 rich.Table 로 출력.
- `agent-loop memory path` — 디렉토리 절대 경로 한 줄 출력.

기존 `run` / `bench` 에 `--no-cross-task` flag — 한 번 실행만 cross_task=False 로 강제.

### 6.3 orchestrator.py
- `__init__` 에서 ContextEngine 생성 시 config 의 global_root / cross_task / max_chars 전달.
- `run()` 마지막에 (final_status 가 stop / max_cycles / max_redo / budget_exceeded / user_aborted 어느 값이든) `context.commit_to_global(summary)` 호출. 예외는 console warning 으로 격리 (절대 run 결과 깨지지 않도록).
- summary 작성 헬퍼 `_build_global_summary(result, total_cost)` 분리.

### 6.4 workers.py
- `_memory_text` 헬퍼가 `MemorySnapshot.render()` 결과를 그대로 사용 (이미 global_patterns 포함). 추가 변경 X.
- prompts/*.md 변경 X (`{memory}` placeholder 그대로).

## 7. Privacy / 보안 원칙

| 항목 | 처리 |
|---|---|
| 코드 (`solution.py`, `plan.md`) | 글로벌에 절대 저장 X. |
| prompt 본문 / LLM 응답 raw text | 글로벌에 저장 X. |
| 사용자 task 설명 (task.md) | `task_md_first_line` 만 (해시 X — UX 우선). 사용자가 민감 정보를 첫 줄에 쓰지 않도록 README 에 명시. |
| `CORE:` 라인 | 사용자가 명시적으로 prefix 한 라인만 누적 (opt-in 시맨틱). |
| Wipe | `agent-loop memory wipe` 한 번에 디렉토리 삭제. typer.confirm 강제. |
| 외부 sync | v0.4 는 단일 호스트 가정. cloud sync 는 없음. |

## 8. Backward compat

- `runtime.cross_task_memory=False` 면 ContextEngine 이 글로벌 IO 0 회. v0.3 동일.
- v0.3 까지의 task 디렉토리 (`memory/core_facts.md` 만 존재) 는 그대로 resume 가능.
- 글로벌 디렉토리가 없으면 `_load_global_patterns()` 가 `""` 반환, snapshot.render 가 `# Global Patterns` 섹션 자체를 안 적음.
- 기존 prompts/*.md 의 `{memory}` placeholder 는 그대로. 새 섹션은 `MemorySnapshot.render()` 안에서 conditional append.
- `judge_result.json` / `solution.json` 등 artifact schema 변경 0.

## 9. 작업 분해

| # | 작업 | 산출물 |
|---|---|---|
| 1 | `docs/plan-v0.4.md` (이 파일) | this |
| 2 | `context.py`: MemorySnapshot.global_patterns + ContextEngine 인자 / 메서드 | core 확장 |
| 3 | `config.py`: Runtime 3 필드 + ENV 3 개 + DEFAULT_TOML 주석 | config 확장 |
| 4 | `orchestrator.py`: ContextEngine 인자 전달 + commit_to_global 호출 | 통합 |
| 5 | `cli.py`: `memory` subcommand 4 개 + `--no-cross-task` flag | UI |
| 6 | `workers.py`: `_memory_text` 가 새 render 그대로 사용 (변경 0 또는 1줄) | wiring |
| 7 | `tests/test_cross_task.py` (10) | core test |
| 8 | `tests/test_memory_cli.py` (5) | CLI test |
| 9 | `tests/test_cross_task_e2e.py` (2) | mock e2e |
| 10 | `README.md` Cross-task memory (v0.4) 섹션 | doc |
| 11 | `docs/architecture.md` global memory 박스 | doc |
| 12 | `progress.txt` 갱신 + `__version__ = "0.4.0"` | release log |

## 10. 성공 기준

1. `pytest -q` 가 v0.3.2 기존 132 + 신규 17+ 모두 green (regression 0).
2. mock fs 에서 두 task 가 같은 `tmp_path/global/` 을 공유할 때, 두 번째 task 의 `ContextEngine.snapshot()` 이 첫 번째 task 가 commit 한 `CORE:` 라인을 포함.
3. dedup: 같은 `CORE:` 라인이 두 task 에서 commit 되어도 patterns.md 에는 1 회만 등장.
4. cross_task=False 일 때 글로벌 IO 0 회 (mock filesystem 에서 file open 호출 0 검증).
5. `agent-loop memory show` / `list` / `path` / `wipe` 4 명령 모두 exit 0 + 정확한 출력.
6. ENV `AGENT_LOOP_RUNTIME_CROSS_TASK_MEMORY=false` 가 config 와 CLI 둘 다 override.
7. `commit_to_global` 가 같은 task_id 두 번 호출되어도 patterns.md / task_index.jsonl 추가 라인 없음 (idempotent).

## 11. 위험 / 한계

| 위험 | 대응 |
|---|---|
| 글로벌 패턴이 너무 길어져 prompt 부풀음 | `cross_task_memory_max_chars=4000` default. snapshot 시 끝(최근)에서 잘라냄. 사용자가 `memory wipe` 로 reset. |
| 동시 task 가 같은 patterns.md 에 race write | `O_APPEND` write + dedup 은 set 검사 — 최악의 경우 동일 라인 중복 1 개 정도. v0.5 에 fcntl.flock 검토. |
| 사용자가 민감 정보를 `CORE:` 에 적음 | `CORE:` 는 명시적 opt-in (judge hint 에서만 사용). README 에 prompt injection 경고 추가. |
| 글로벌 메모리에 노이즈가 끼면 새 task 품질 저하 | `agent-loop memory wipe` + 향후 score 가중 retrieval (v0.5). |
| 사용자가 글로벌 디렉토리 손으로 편집 | 허용. 라인 형식만 맞으면 OK (사용자가 "주인"). |

## 12. 결정 사항 (확정)

- **default ON** — `cross_task_memory: bool = True`. 사용자가 끄려면 명시 (privacy 트레이드: 학습 누적이 본 기능이라 default ON, 끄기 쉬움).
- **위치** — `~/.agent-loop/global/`. `~/.agent-loop/config.toml` 과 같은 디렉토리. 자동 생성.
- **patterns.md 만 prompt 노출**, `task_index.jsonl` 은 audit / `memory list` 표시 전용 — prompt 에 포함 X (스코어 / cycles 정보가 LLM 판단에 노이즈).
- **dedup 은 exact match**. v0.5 에서 fuzzy / semantic 검토. KISS 우선.
- **MCP server 는 v0.4.1** — 이 PR 은 hooks 만. `commit_to_global` / `_load_global_patterns` 가 server 의 read/write 진입점.
- **`--no-cross-task`** 는 한 번 실행만 끄는 ergonomics flag. `cross_task_memory=false` config 는 영구 설정.
- **idempotent** — 같은 task_id 두 번 commit 시 task_index.jsonl 에 중복 안 씀 (set 검사).
- **error isolation** — `commit_to_global` 예외는 절대 run 결과 깨뜨리지 않음. console warning 만.

## 13. 다음 단계 (이 plan 후)

1. (이 PR) 작업 #2~#12 완료 → progress.txt 갱신, push X.
2. (v0.4.1) `src/agent_loop/mcp_server.py` 신규 — Model Context Protocol 표준에 맞춰 `tools/list`, `resources/list`, `resources/read` 노출. `commit_to_global` / `_load_global_patterns` 가 read/write 백엔드.
3. (v0.5) score 가중 retrieval — `task_index.jsonl` 의 weighted_score / cycles 를 활용해 새 task 의 prompt 에 더 관련도 높은 패턴만 inject.
4. (v0.5+) 글로벌 메모리 dashboard — `agent-loop memory stats` / `agent-loop memory diff <task1> <task2>`.
