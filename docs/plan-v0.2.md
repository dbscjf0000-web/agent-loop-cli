# agent-loop-cli v0.2 — Context Engine 계획

## 1. 목표 (한 문장)

v0.1.1의 단일 `memory.txt`를 **3-tier memory + Sensors + Compactor** 로 대체해
phase worker가 받는 컨텍스트의 품질을 측정·압축할 수 있는 첫 동작 가능한 ContextEngine
을 추가한다 (다축 Verify Engine은 별도 워커가 진행).

## 2. 배경

- v0.1.1: 모든 phase가 동일한 `memory.txt` 한 파일을 raw로 prompt 에 주입.
  cycle 이 누적되면 양도 늘고 중복도 늘어 prompt 가 부풀고, 어떤 항목이
  새 cycle 에 의미 있는지 측정할 방법이 없음.
- 플러그인 시절 메모는 "judge hint 한 줄 누적" 수준이었음.
  CLI v0.2 부터는 자체 디렉토리(`memory/`) + 압축기 + 센서 셋을 들여 v0.3+ 의
  multi-judge / multi-strategy 가 의미 있는 컨텍스트 위에서 동작하도록 한다.

## 3. 비기능 요구사항 (불변 원칙)

| 원칙 | 의미 |
|---|---|
| **Backward compat** | v0.1 task (`memory.txt` 만 존재) 도 `agent-loop resume` 으로 이어 돌아가야 한다. |
| **Stateless workers** | ContextEngine 은 메모리 상에서 캐시하지 않고, 매 phase 시작에 디스크에서 fresh 로 snapshot 만든다. |
| **No new deps** | rule-based 압축기 + 단순 휴리스틱 sensor 만. LLM 호출 없음. |
| **Idempotent init** | resume 에서도 `ContextEngine.init()` 이 무해해야 한다. |
| **Prompt 호환** | 기존 prompts/*.md 가 그대로 동작 (`{memory}` placeholder 유지). |

## 4. v0.2 범위 (스코프)

### 포함 (이 워커)
- 3-tier memory layout: `history.jsonl` (audit) + `episodic.md` (요약) + `core_facts.md` (영속 패턴)
- 신규 모듈 `src/agent_loop/context.py` — `ContextEngine` 클래스 단일
- `TaskDir` 확장: `memory_dir()` + `memory/` 디렉토리 자동 생성
- `workers.py` 5 개 phase 모두 `ContextEngine.snapshot()` 사용 + `append_history()` 후처리
- `orchestrator.py` 가 cycle 끝마다 `compact()` + `sensors()` 호출 → `metrics.jsonl` 의 `quality` 필드
- 마이그레이션: 기존 `memory.txt` → `memory/core_facts.md` 일회성 복사 + `memory.txt.v0_1.bak`
- 단위 테스트 (`tests/test_context.py`) + e2e mock 통합 (`tests/test_context_e2e.py`)
- `README.md` "Context Engine (v0.2)" 섹션 + `docs/architecture.md` 갱신

### 제외 (다음 워커 / 향후)
- 다축 Verify Engine (별도 워커가 V phase rubric 분리)
- LLM 기반 Compactor (v0.3 에서 옵션 B)
- LLM 기반 contradiction / relevance scoring (v0.3)
- multi-strategy / multi-judge (v0.3 그대로)
- cross-task memory store (v0.4)
- MCP server mode (v0.4)

## 5. 아키텍처 (4 → 4 layer 유지, Phase Worker leaf 추가)

```
CLI (typer)
   |
   v
Orchestrator (R->P->I->V->J + rollback + checkpoint + compact())
   |
   v
Phase Workers (R/P/I/V/J)
   |  uses
   |        +------ Model Router (litellm | cursor-agent CLI)
   |        +------ State Store (TaskDir)
   |        +------ Context Engine  ← NEW (snapshot + append_history)
   v
.agent_loop/<id>/
   ├── task.md
   ├── memory.txt           (legacy, 마이그레이션 후 .v0_1.bak)
   ├── memory/              ← NEW
   │   ├── history.jsonl    (raw audit, append-only)
   │   ├── episodic.md      (Compactor 산출, prompt 노출)
   │   └── core_facts.md    (영속 패턴, prompt 노출)
   ├── workspace/
   ├── artifacts/
   ├── checkpoints/
   └── telemetry/metrics.jsonl  (`quality` 필드 추가)
```

ContextEngine 은 **별도 layer 가 아니라 workers/orchestrator 가 들고 다니는 leaf
컴포넌트**이다. Model Router 처럼 phase 워커가 호출만 한다.

## 6. 모듈 / 인터페이스

### 신규 `src/agent_loop/context.py`

```python
@dataclass
class MemorySnapshot:
    episodic: str
    core_facts: str
    history_count: int

class ContextEngine:
    def __init__(self, task_dir: TaskDir) -> None: ...
    def init(self) -> None: ...
    def append_history(self, record: dict) -> None: ...
    def snapshot(self) -> MemorySnapshot: ...
    def compact(self, *, force: bool = False) -> dict: ...
    def sensors(self) -> dict: ...
```

- `init()` — `memory/` 디렉토리 + `history.jsonl` / `episodic.md` / `core_facts.md`
  를 생성. 이미 있는 `memory.txt` (v0.1) 은 `core_facts.md` 가 비어 있을 때만
  내용을 복사하고 `memory.txt.v0_1.bak` 백업을 남긴다. 호출 여러 번 안전.
- `append_history(record)` — `{cycle, phase, timestamp, summary, score?, hint?}`
  형태 dict 를 `history.jsonl` 에 한 줄 append.
- `snapshot()` — phase prompt 입력용. `episodic.md` 와 `core_facts.md`
  둘 다 읽어서 `MemorySnapshot` 으로 묶음.
- `compact(force=False)` — rule-based 압축. 기본 트리거: cycle 끝 또는
  `episodic.md > 6KB`. 출력은 `{ "size_before", "size_after", "lines_kept",
  "core_extracted" }` 등 변화 dict.
- `sensors()` — 휴리스틱:
  - `duplicate_ratio` — `episodic.md` 라인 중 중복 비율 (lower-cased 비교)
  - `contradiction_count` — v0.2 placeholder = 0 (LLM 도입 후 활성)
  - `staleness_age_cycles` — `history.jsonl` 의 가장 오래된 cycle 부터 최신 cycle 까지 거리
  - `relevance_score` — `episodic.md` 길이 기반 bounded heuristic
    (예: `1.0 - clamp(len/8000, 0, 1)`. 길수록 낮음 = 압축 신호)

### `src/agent_loop/state.py` 확장

```python
class TaskDir:
    def memory_dir(self) -> Path:        # NEW: self.path / "memory"
        ...
    def init(self) -> None:
        # 기존 + memory_dir() 추가 생성
        ...
    # memory_md_path() 는 그대로 유지 (deprecated 표시) — backward compat
```

### `src/agent_loop/workers.py` 수정

- `_load_memory()` (가칭) 헬퍼: 기존 `memory_md_path().read_text()` 자리에서
  `ContextEngine.snapshot()` 결과를 `f"# Episodic\n{snap.episodic}\n\n# Core Facts\n{snap.core_facts}"`
  로 합쳐 `prompt_vars["memory"]` 에 넣음.
- 각 phase 끝에 `context.append_history({...})`. `summary` 필드는 산출물의 첫 ~200 자.
- worker 가 `ContextEngine` 인스턴스를 받지 않게 하려면 함수 시그니처는 그대로 두고
  내부에서 `ContextEngine(task_dir)` 을 즉석 생성 → 비용 0 (디스크 read 만).
  대안: 시그니처에 `context: ContextEngine | None = None` 추가하고 None 이면 lazy 생성.
  → **즉석 생성 채택** (worker 시그니처 보존, stateless 원칙 유지).

### `src/agent_loop/orchestrator.py` 수정

- `__init__` 에서 `self.context = ContextEngine(task_dir); self.context.init()`.
- 매 cycle 끝 (judge 직후) 에 `self.context.compact()` + `quality = self.context.sensors()`.
- `metrics.jsonl` 에 `{ "phase": "_cycle_quality", "quality": {...}, ... }` 한 줄 추가.

### `src/agent_loop/cli.py`

- 변경 없음 (resume 도 ContextEngine.init 이 idempotent 라 자동 동작).

## 7. 마이그레이션 정책

1. `agent-loop run/resume` 양쪽에서 `ContextEngine.init()` 이 호출됨.
2. `memory_dir()` 이 비어 있고 `memory.txt` 가 비어 있지 않으면:
   - `core_facts.md` 가 비어 있을 때만 `memory.txt` 내용을 그대로 복사.
   - `memory.txt` → `memory.txt.v0_1.bak` 으로 rename (있으면 덮어쓰기 X, 다른 이름).
   - 빈 `memory.txt` 새로 touch 해서 v0.1 코드가 읽어도 안 깨지도록.
3. `episodic.md` 는 처음엔 비어 있고 첫 cycle 이 끝난 뒤 `compact()` 가 채움.
4. v0.2 task 에 `memory.txt` 가 없으면 단순히 새 디렉토리 생성만.

## 8. 작업 분해

| # | 작업 | 산출물 |
|---|---|---|
| 1 | `docs/plan-v0.2.md` (이 파일) | this |
| 2 | `state.py` 에 `memory_dir()` + init 보강 | TaskDir 확장 |
| 3 | `context.py` 신규 (ContextEngine 클래스 단일) | 5 메서드 |
| 4 | `workers.py` 5 phase 가 snapshot + append_history 사용 | 인터페이스 변경 X |
| 5 | `orchestrator.py` 가 cycle 끝마다 compact + sensors 기록 | metrics quality 필드 |
| 6 | `tests/test_context.py` (라운드트립/마이그레이션/sensors) | 5+ tests |
| 7 | `tests/test_context_e2e.py` (mock 1 cycle 통합) | 1+ test |
| 8 | `README.md` Context Engine 섹션, `docs/architecture.md` Context Engine 자리 갱신 | doc |
| 9 | `progress.txt` Codebase Patterns + 새 항목 | log |

## 9. 성공 기준

1. `pytest -q` 가 v0.1.1 기존 32 + 새 테스트 모두 green (regression 0).
2. mock e2e 가 1 cycle 끝까지 진행되고 `memory/history.jsonl` / `episodic.md` /
   `core_facts.md` 셋 다 파일이 존재 + 비어 있지 않다 (history 는 phase 수만큼).
3. v0.1.1 의 빈 `memory.txt` task 디렉토리에 `ContextEngine.init()` 을 한 번 돌려도
   기존 파일이 손상되지 않고 `memory/` 가 새로 생긴다 (idempotent).
4. v0.1.1 의 채워진 `memory.txt` task 에 `init()` 을 돌리면 `core_facts.md` 에 동일
   내용이 들어가고 `memory.txt.v0_1.bak` 가 남는다.
5. `agent-loop resume <id>` 가 v0.2 task 를 정상 이어 간다 (단위 테스트로 검증).
6. `metrics.jsonl` 마지막 줄 (`_cycle_quality`) 에 `quality.duplicate_ratio` 등
   4 개 키가 존재.

## 10. 위험 요소 & 대응

| 위험 | 대응 |
|---|---|
| `compact()` 가 처음부터 너무 공격적으로 잘라 정보 손실 | rule-based 는 "cycle 별 1줄 + best score 갱신" 수준 (보수적). v0.3 LLM 으로 점진 강화. |
| 마이그레이션이 사용자 작성 `memory.txt` 를 덮어씀 | `core_facts.md` 가 비어 있을 때만 복사. `.v0_1.bak` 항상 남김. |
| sensor 가 LLM 없이 너무 단순해 의미 없음 | v0.2 는 placeholder 라고 README 에 명시. dashboard 에서 추세만 봐도 가치 있음. |
| `append_history` race (두 워커 동시 작성) | `with open(..., 'a')` 한 번 열어 1 줄만 쓰면 POSIX append 보장. 동시 호출 케이스는 v0.1 부터 없음. |

## 11. 다음 단계 (이 plan 후)

1. (이 워커) 작업 #2 ~ #9 완료 → progress.txt 갱신.
2. (다음 워커) 다축 Verify Engine: `verify` phase 의 rubric 을 `verify.py` 또는
   별도 모듈로 분리, ContextEngine 의 `core_facts.md` 에 axes 별 누적 hint 를 쓰도록 연결.
3. (v0.3) Compactor 옵션 B (LLM 기반) + sensor 의 LLM-backed `contradiction_count`,
   `relevance_score` 활성화.

## 12. Verify Engine 추가 (v0.2 두 번째 트랙, 워커 #2)

### 12.1 목표 (한 문장)
v0.1 단일 LLM verify 호출을 **rubric 기반 다축 평가**로 교체하되, programmatic
evaluator (pytest/benchmark/ast_grep) 를 ground truth 로 우선시하고 LLM rubric 은
soft fallback 으로 둔다. v0.1 task 는 그대로 (rubric 없으면 legacy 경로).

### 12.2 모듈 구조
```
src/agent_loop/
├── verify_types.py          # AxisScore, VerifyResult dataclasses (cycle-free)
├── verify_engine.py         # VerifyEngine + yaml_to_rubric
└── evaluators/
    ├── __init__.py          # EVALUATORS registry + evaluate_axis dispatch
    ├── pytest_runner.py     # assert 라인 단위 pass/total
    ├── benchmark.py         # timeit + median + threshold linear falloff
    ├── ast_grep.py          # 미니 DSL (count/in/not_in)
    └── llm_rubric.py        # call_model(verify) 후 JSON 파싱
```

### 12.3 데이터 클래스 (verify_types.py)
- `AxisScore(name, score 0..1, weight, evaluator, evidence, is_ground_truth, raw?)`
- `VerifyResult(axes: list[AxisScore], weighted_score, summary)` — summary 는 200 char cap.

### 12.4 VerifyEngine 동작
- `evaluate(rubric)` → 모든 axis 를 `evaluators.evaluate_axis` 로 디스패치.
- weighted_score = Σ(score × weight) / Σ(weight). axis 가 없으면 0.0 + summary "no axes evaluated".
- 각 evaluator 는 예외 시 score=0 + evidence (run 깨지지 않음).

### 12.5 워커 통합 (`workers.py`)
- `run_verify` 가 `task_dir.artifact_path("rubric.json")` 검사 → 있으면 `_run_verify_with_rubric`
  → `VerifyEngine.evaluate` → `solution.json` 에 axes/weighted_score/summary 저장 + ContextEngine
  history append (LLM 호출 0).
- 없으면 `_run_verify_llm_legacy` (v0.1 본체 그대로 보존).
- ModelResponse(model="(verify_engine: rubric)", cost=0, latency=실측) 반환.

### 12.6 cli.py 변경
- `bench` 명령에서 yaml `success_criteria` → `yaml_to_rubric` → `td.write_artifact("rubric.json", ...)`.
- 다른 명령은 변경 없음.

### 12.7 yaml_to_rubric 매핑
- `measure: wall_clock_seconds`/`speedup_ratio` → `benchmark`
- `measure: source_inspection` → `ast_grep`
- `test:` 있음 → `pytest`
- `rule:` 있음 → `ast_grep`
- 그 외 → `llm_rubric`
- benchmark `stmt` 미지정 시 `target` 에서 첫 `name(...)` 정규식 추출.

### 12.8 backward compat
- v0.1 솔루션 `{"score": ...}` 도 judge first-cycle 분기에서 `weighted_score` 우선,
  fallback `score` (workers.run_judge).
- legacy 경로는 schema 변경 없이 보존.

### 12.9 새 테스트
- `tests/test_evaluators.py` (11): pytest 통과/부분실패/import 오류, benchmark threshold/감소, ast_grep
  통과/위반/count, llm_rubric monkeypatch, evaluate_axis dispatch (unknown / pytest).
- `tests/test_verify_engine.py` (4): legacy 경로 (no rubric), rubric 경로 (no LLM), weighted 산술,
  empty rubric.
- `tests/test_yaml_to_rubric.py` (3): n_queens (pytest+benchmark), binary_search (ast_grep),
  sort_tuning (speedup_ratio).
- `tests/test_verify_e2e.py` (1): mock e2e — rubric 작성 → run_verify → solution.json
  schema + ContextEngine history 기록 검증.
