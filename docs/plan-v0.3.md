# agent-loop-cli v0.3 — Multi-judge Consensus + Multi-strategy Plans 계획

## 1. 목표 (한 문장)

v0.2 의 단일 LLM judge / 단일 plan 호출을 **N-vendor 병렬 fan-out + 합의(consensus)
또는 선별(selection)** 로 교체해, 한 vendor 의 편향에 노출된 채 cycle 을 진행하던
구조를 다중 의견으로 보강한다. 두 트랙은 같은 fan-out 추상을 공유하되,
**multi-judge 는 워커 B 가 v0.3 에 본구현**, **multi-strategy 는 워커 C 가 다음에
이어서 구현**한다 (이 문서가 양쪽 design 합의서).

## 2. 비기능 요구사항 (불변 원칙)

| 원칙 | 의미 |
|---|---|
| **Backward compat** | 기존 `agent-loop run` (v0.1 / v0.2 task) 가 코드 한 줄 수정 없이 그대로 동작. 단일 judge / 단일 plan 호출이 default. |
| **Cross-vendor 우선** | 같은 vendor N 개 fan-out 은 **목적상 무의미**. design / config 가 다른 provider 묶음을 자연스럽게 권장. |
| **No new deps** | `concurrent.futures` (stdlib) 만 사용. ThreadPoolExecutor 가 CLI subprocess 의 IO bound 에 적합. |
| **Fail soft** | N 개 judge 중 일부 실패 시 partial consensus. 모두 실패 시 single-judge fallback (명시적 에러 X). |
| **Stateless 유지** | Engine 은 `(TaskDir, Config)` 만 보관. ThreadPool 은 매 호출마다 ephemeral. |
| **Schema 호환** | `judge_result.json` / `solution.json` 의 **기존 키는 모두 유지**, 새 키는 optional (`consensus`, `proposals`). |

## 3. 범위 (스코프)

### 포함 (워커 B = 이 PR)
- `src/agent_loop/judge_engine.py` 신규 — `JudgeEngine` 클래스, `IndividualJudgement` /
  `ConsensusResult` dataclass.
- `src/agent_loop/workers.py` `run_judge` 분기 추가 — multi 모드면 `JudgeEngine.consensus`,
  아니면 `_run_judge_single` (기존 본체 그대로 보존, first-cycle short-circuit 포함).
- `src/agent_loop/config.py` 확장 — `JudgeSpec`, `Runtime.judges: list[JudgeSpec] | None`,
  `AGENT_LOOP_RUNTIME_JUDGES` 환경 변수 override (콤마 구분, weight=1.0).
- `src/agent_loop/cli.py` `run` / `bench` 에 `--judge` 반복 가능 플래그 추가, `config show`
  가 `runtime.judges` 표시.
- `src/agent_loop/orchestrator.py` 미세 — `metrics.jsonl` 의 judge 행에 `n_judges` /
  `votes_action` 기록 (action 분기는 그대로 `judge_result.json["action"]`).
- 단위 테스트: `tests/test_judge_engine.py` (8+) + `tests/test_workers_multijudge.py` (3+).
- `README.md` Multi-judge (v0.3) 섹션 + `docs/architecture.md` Judge Engine 박스 추가.
- `progress.txt` Codebase Patterns + 새 항목.

### 제외 (워커 C = 다음 PR)
- `src/agent_loop/strategy_engine.py` 신규 — Plan 단계 fan-out + Selector.
- `workers.py:run_plan` 분기, `Runtime.strategies` config, `--strategy` CLI flag.
- multi-strategy 단위 테스트 + e2e mock.
- (이 문서 5절에 design 명세는 모두 포함 — 워커 C 가 그대로 구현 가능.)

### 향후 (v0.4+)
- 다른 phase 의 multi-fan-out (verify / research). 필요성이 검증되기 전까지 보류.
- Judge / Strategy 결과를 ContextEngine.core_facts.md 에 누적해 cross-task 학습.
- LLM 기반 Compactor + LLM-backed sensors (v0.2 plan 11절 그대로).
- MCP server mode, cross-task memory store.

## 4. 아키텍처 (변경 요약)

```
                   +-------------------------+
   user / CI ----->|  CLI (typer)            |
                   +-----------+-------------+
                               |
                               v
                   +-------------------------+
                   |  Orchestrator           |
                   +-----------+-------------+
                               |
                               v
              +---------------------------------------------+
              |  Phase Workers                              |
              |   run_research / run_plan / run_implement / |
              |   run_verify  / run_judge                   |
              +-+-----------+-----------+-----------+-------+
                |           |           |           |
                v           v           v           v
   +----------------+ +-----------+ +-------+ +------------+
   | Model Router  | | Context   | | State | | Verify     |
   | litellm + 3 CLIs| Engine    | | Store | | Engine     |
   +-------+--------+ +-----+-----+ +---+---+ +-----+------+
           ^                                         ^
           |  (every leaf fans out below)            |
           +------------------+----------------------+
                              |
              +---------------+----------------+
              |   Judge Engine (v0.3, NEW)     |
              |     - JudgeEngine.consensus()  |
              |     - ThreadPoolExecutor       |
              |     - IndividualJudgement      |
              |     - ConsensusResult          |
              +--------------------------------+
              +--------------------------------+
              |   Strategy Engine (v0.3, NEW)  |  (worker C, design only)
              |     - StrategyEngine.fan_out() |
              |     - PlanProposal             |
              |     - SelectorResult           |
              +--------------------------------+
```

JudgeEngine 과 StrategyEngine 은 **레이어 추가가 아니라 worker 가 들고 다니는 leaf
컴포넌트**다 (Context / Verify Engine 과 동일 패턴). 각각 하나의 phase function 만
직접 사용한다 (`run_judge` / `run_plan`).

## 5. Multi-judge 상세 설계 (워커 B 본구현)

### 5.1 모듈 구조

```
src/agent_loop/judge_engine.py
   ├── @dataclass IndividualJudgement
   ├── @dataclass ConsensusResult
   ├── class JudgeEngine
   │     ├── __init__(task_dir, config)
   │     ├── consensus(judges) -> ConsensusResult
   │     ├── _build_prompt() -> str         # judge.md template 한 번만 렌더
   │     ├── _call_one(spec, prompt) -> IndividualJudgement
   │     └── _aggregate(individuals) -> ConsensusResult
   └── result_to_dict(consensus) -> dict    # judge_result.json 저장용
```

### 5.2 Config schema

`Runtime.judges` 가 추가된다 (default `None`). pydantic 모델.

```python
class JudgeSpec(BaseModel):
    provider: str       # "claude/default", "gemini/gemini-2.5-flash", "cursor/auto", ...
    weight: float = 1.0

class Runtime(BaseModel):
    sandbox: bool = True
    max_cycles: int = 10
    max_redo: int = 3
    judges: list[JudgeSpec] | None = None
```

TOML 작성 형식 두 가지 모두 허용:

```toml
# 가중치 포함 형식 (권장)
[[judges]]
provider = "claude/default"
weight = 1.0

[[judges]]
provider = "gemini/gemini-2.5-flash"
weight = 1.0

[[judges]]
provider = "cursor/auto"
weight = 1.0
```

또는 단순 리스트 (모두 weight=1.0):

```toml
[runtime]
judges = ["claude/default", "gemini/gemini-2.5-flash", "cursor/auto"]
```

`load_config` 가 둘 다 파싱하도록 `_normalize_judges()` 헬퍼를 둔다 (str list →
JudgeSpec list 변환).

환경 변수: `AGENT_LOOP_RUNTIME_JUDGES="claude/default,gemini/gemini-2.5-flash,cursor/auto"`
(콤마 구분, weight 모두 1.0).

CLI: `--judge` 반복 가능 플래그가 있으면 `cfg.runtime.judges` 를 덮어쓰기.

### 5.3 호출 흐름 (workers.run_judge)

```python
def run_judge(task_dir, config) -> ModelResponse:
    if config.runtime.judges:
        return _run_judge_multi(task_dir, config)
    return _run_judge_single(task_dir, config)   # 기존 본체 (first-cycle short-circuit 포함)
```

`_run_judge_multi`:
1. **first-cycle short-circuit 보존**: `best_solution.json` 이 없으면 single 경로
   그대로 위임 → multi-judge 비용 0.
2. JudgeEngine 생성 → `consensus(config.runtime.judges)`.
3. ConsensusResult → `judge_result.json` 저장 (top-level: `better` / `action` /
   `scores` / `hint` / `reason` + 새 `consensus` 필드).
4. ContextEngine.append_history({...}).
5. ModelResponse 반환 (cost_usd = sum, latency_s = max(individual)).

### 5.4 JudgeEngine.consensus

병렬 호출:
```python
with ThreadPoolExecutor(max_workers=len(judges)) as ex:
    futures = {ex.submit(self._call_one, spec, prompt): spec for spec in judges}
    individuals: list[IndividualJudgement] = []
    for fut in as_completed(futures):
        try:
            individuals.append(fut.result(timeout=cli_timeout))
        except Exception as e:
            spec = futures[fut]
            individuals.append(IndividualJudgement(
                provider=spec.provider, weight=spec.weight,
                better=False, action="stop", weighted_score=None,
                hint="", reason="", raw={}, error=str(e)[:500],
            ))
```

`_call_one`: prompt 동일, provider 만 임시 override.
- `Config` 복제 후 `cfg.models.judge = spec.provider`.
- `models.call_model("judge", prompt, system, cfg, workspace=...)` 호출.
- 응답 파싱: 기존 `_extract_json` 재사용. JSON 깨지면 `error` 채우고 better=False 처리.

### 5.5 Aggregation 알고리즘

```python
def _aggregate(individuals) -> ConsensusResult:
    valid = [i for i in individuals if i.error is None]
    if not valid:
        # 전부 실패 -> caller 가 fallback to single
        raise AllJudgesFailed(individuals)

    # action: weighted majority. 동률이면 'stop' 보수적 우선.
    votes_action: dict[str, float] = {}
    for j in valid:
        votes_action[j.action] = votes_action.get(j.action, 0.0) + j.weight
    max_w = max(votes_action.values())
    top_actions = [a for a, w in votes_action.items() if w == max_w]
    action = "stop" if "stop" in top_actions else sorted(top_actions)[0]

    # better: weighted true/false 합. >0 이면 True.
    votes_better = {"true": 0.0, "false": 0.0}
    for j in valid:
        votes_better["true" if j.better else "false"] += j.weight
    better = votes_better["true"] > votes_better["false"]

    # weighted score: weighted_score 가 있는 judge 만 평균
    weighted_avg = None
    scored = [(j.weight, j.weighted_score) for j in valid if j.weighted_score is not None]
    if scored:
        total_w = sum(w for w, _ in scored)
        weighted_avg = sum(w * s for w, s in scored) / total_w if total_w > 0 else None

    hint = "\n---\n".join(j.hint for j in valid if j.hint)
    reason = "\n---\n".join(f"[{j.provider}] {j.reason}" for j in valid if j.reason)

    return ConsensusResult(
        better=better, action=action,
        scores={"weighted": weighted_avg, ...},
        hint=hint, reason=reason,
        individual=individuals,
        n_judges=len(individuals),
        votes_action=votes_action,
        votes_better=votes_better,
    )
```

Rule:
- **action**: weight majority. 동률이면 `stop` 우선 (보수). `stop` 이 없으면 알파벳 순
  (`redo_P` < `redo_R`) — 결정성 확보.
- **better**: weight 합 비교. true 합 > false 합 ⇒ `True`. 동률이면 `False`
  (보수: 새 제안이 명백히 우세하지 않으면 promote 하지 않음).
- **weighted score**: judge 가 보고한 `scores.this_cycle` (없으면 `weighted_score`)
  의 가중 평균. 한 judge 도 점수를 안 주면 None.
- **hint / reason**: 보존을 위해 concat (`\n---\n`). 프롬프트 다음 cycle 에 들어갈 때
  중복 제거는 ContextEngine 의 compact() 에 위임.

### 5.6 Output schema 확장

`judge_result.json` (multi 모드):

```json
{
  "better": true,
  "action": "stop",
  "scores": {
    "weighted": 0.85,
    "this_cycle": 0.85,
    "best": 0.78,
    "delta": 0.07
  },
  "hint": "...\n---\n...",
  "reason": "[claude/default] ...\n---\n[gemini/...] ...",
  "consensus": {
    "n_judges": 3,
    "votes_action": {"stop": 2.0, "redo_P": 1.0},
    "votes_better": {"true": 2.0, "false": 1.0},
    "individual": [
      {
        "provider": "claude/default", "weight": 1.0,
        "better": true, "action": "stop", "weighted_score": 0.88,
        "hint": "...", "reason": "...", "error": null
      },
      {
        "provider": "gemini/gemini-2.5-flash", "weight": 1.0,
        "better": true, "action": "stop", "weighted_score": 0.83,
        "hint": "...", "reason": "...", "error": null
      },
      {
        "provider": "cursor/auto", "weight": 1.0,
        "better": false, "action": "redo_P", "weighted_score": 0.55,
        "hint": "...", "reason": "...", "error": null
      }
    ]
  }
}
```

`scores.this_cycle` / `scores.best` / `scores.delta` 는 single 모드와 동일 의미로
계산해 채운다 (orchestrator 의 `_read_judge_result` 가 그대로 동작).

backward compat: `consensus` 필드 부재면 single judge 모드로 간주 (기존 코드 손대지
않음).

### 5.7 Failure modes

| 상황 | 처리 |
|---|---|
| 한 judge timeout / RuntimeError | `IndividualJudgement.error` 채움. 나머지로 partial consensus. |
| JSON 파싱 실패 | `error="unparseable JSON"`, better=False, action="stop" 처리. |
| 모든 judge 실패 | `JudgeEngine.consensus` 가 `_run_judge_single` 위임 (fallback). reason 에 `"all judges failed"` 기록. |
| `judges` 가 빈 리스트 | config validation 단계에서 None 으로 강등 (= single 모드). |
| same provider 중복 | 허용. 가중치만 합산. (사용자 명시적 의도 존중.) |

### 5.8 Orchestrator 통합

`orchestrator.py` 의 변경은 최소:
- `_read_judge_result()` 가 받은 dict 의 `consensus` 필드를 그대로 통과시키면 됨.
- 매 cycle 끝 `metrics.jsonl` 에 추가 행 (judge phase row 안에 nest 가 아니라 별행):
  ```json
  {"phase": "judge", "cycle": N, "n_judges": 3,
   "votes_action": {"stop": 2.0, "redo_P": 1.0},
   "votes_better": {"true": 2.0, "false": 1.0}, ...}
  ```
- judge 의 `action` / `better` 분기 로직은 변경 없음 (consensus 결과가 이미 single 과
  동일한 키로 들어감).

### 5.9 CLI 표면

```bash
# explicit judges via flag (반복 가능)
agent-loop run "..." \
  --judge claude/default \
  --judge gemini/gemini-2.5-flash \
  --judge cursor/auto

# bench 도 동일
agent-loop bench n_queens --judge claude/default --judge gemini/gemini-2.5-flash

# config show 가 multi-judge 표시
agent-loop config show
# {"runtime": {..., "judges": [{"provider": "claude/default", "weight": 1.0}, ...]}}
```

`--judge` 가 비어 있으면 config 의 `runtime.judges` 사용.
`runtime.judges` 도 비어 있으면 single 모드 (`config.models.judge`).

### 5.10 단위 테스트 (mock 만)

`tests/test_judge_engine.py`:
1. **happy path 3 judges** — claude/gemini/cursor 모두 better=true, action=stop →
   consensus better=true / action=stop / weighted_avg 평균 정확.
2. **action majority** — 2:1 (stop : redo_P) → action=stop, votes_action={stop:2, redo_P:1}.
3. **action tie 보수** — 1:1 (stop : redo_P) → action=stop (stop 우선).
4. **action tie no stop** — 1:1 (redo_R : redo_P) → action=redo_P (알파벳 순).
5. **weight 적용** — claude w=2, gemini w=1, cursor w=1, claude 만 redo_P 나머지 stop →
   votes_action = {stop:2, redo_P:2} → tie → stop.
6. **partial failure** — claude OK, gemini timeout, cursor OK → 2 valid 로 consensus.
   `IndividualJudgement.error` 가 gemini 행에 채워짐.
7. **all fail** — 3 judges 모두 RuntimeError → `AllJudgesFailed` raise (caller 가 fallback).
8. **score weighted average** — (0.9, w=2), (0.6, w=1) → weighted_avg = 0.8.
9. **score None mix** — 한 judge 가 score 안 줌 → 나머지로만 평균.
10. **better tie 보수** — true:false = 1:1 (가중치) → better=False.

`tests/test_workers_multijudge.py`:
1. `run_judge` single 모드 (judges=None) → `_run_judge_single` 그대로 호출 (기존 동작).
2. `run_judge` multi 모드 + first-cycle (best_solution 없음) → single 모드로 위임,
   ThreadPool 호출 0.
3. `run_judge` multi 모드 + 정상 cycle → JudgeEngine.consensus 호출, judge_result.json
   에 `consensus` 키 존재.

총 신규 13 tests 예상.

### 5.11 prompts/judge.md 변경

**변경 없음.** 동일 프롬프트를 N 개 provider 에 그대로 보냄. 단지 `models.judge` 가
spec.provider 로 임시 override 될 뿐. JudgeSpec 별 prompt 차별화는 v0.4 검토.

## 6. Multi-strategy 상세 설계 (워커 C 본구현, 이 문서는 design only)

### 6.1 핵심 아이디어

Plan 단계에서 N 개 plan 을 병렬 fan-out → Selector 가 1 개 골라 implement 로 보냄.
Multi-judge 가 "하나의 결과에 N 개 의견" 이라면, multi-strategy 는 "N 개 결과 중 1
개 선택". Selector 는 v0.3.0 에서는 휴리스틱 + 단일 LLM, v0.4 에서 multi-judge
재사용 가능.

### 6.2 모듈 구조 (워커 C)

```
src/agent_loop/strategy_engine.py
   ├── @dataclass StrategySpec       # provider, weight (judge 와 평행)
   ├── @dataclass PlanProposal       # provider, plan_text, raw, error
   ├── @dataclass SelectorResult     # winner_index, reason, scores
   ├── class StrategyEngine
   │     ├── __init__(task_dir, config)
   │     ├── fan_out(specs, prompt) -> list[PlanProposal]
   │     ├── select(proposals) -> SelectorResult
   │     │      heuristic: 길이 + 코드 fence 개수 + LLM rubric (cfg.models.plan)
   │     └── execute(specs) -> tuple[PlanProposal, SelectorResult]   # all-in-one
```

### 6.3 Config schema

```python
class StrategySpec(BaseModel):
    provider: str       # "claude/default", "cursor/auto", ...
    weight: float = 1.0  # selector tie-break

class Runtime(BaseModel):
    ...
    strategies: list[StrategySpec] | None = None
```

CLI: `--strategy` 반복 가능. ENV: `AGENT_LOOP_RUNTIME_STRATEGIES`.

### 6.4 호출 흐름 (workers.run_plan, 워커 C 가 작성)

```python
def run_plan(task_dir, config) -> ModelResponse:
    if config.runtime.strategies:
        return _run_plan_multi(task_dir, config)
    return _run_plan_single(task_dir, config)   # 기존 본체
```

`_run_plan_multi`:
1. 같은 prompt 를 N strategies 에 ThreadPoolExecutor 로 fan-out.
2. SelectorResult 결정 (heuristic + 단일 LLM call).
3. winner 의 plan_text 를 `plan.md` 에 그대로 저장 (downstream 호환).
4. 모든 proposal 을 `proposals.json` 에 저장 (audit).
5. SelectorResult 를 `plan_selector.json` 에 저장.
6. ContextEngine.append_history({phase:"plan", ..., "selected_provider": ..., "n_proposals": ...}).
7. ModelResponse 반환 (cost_usd = sum, latency_s = max).

### 6.5 Selector 알고리즘 (v0.3.0)

heuristic + LLM 혼합:
- **structural score** (LLM 호출 0): 길이 (clamp 200..4000), 코드 fence 개수 ≥1,
  step 수 (`^\d+\.` 매치 수), 헤더 수 (`^#`).
- **LLM rubric**: `cfg.models.plan` 에 "어느 plan 이 가장 actionable / 구체적이냐"
  질문 1 회. JSON `{winner_index, reason, score: list[float]}`. 비용은 1× plan
  call (overhead 작음).
- 최종: `0.6 * llm_score + 0.4 * structural` (가중 합). tie 면 `weight` 큰 spec 우선,
  여전히 tie 면 첫 spec.

v0.4 옵션: Selector 자체를 multi-judge 로 교체 (N voting → 합의로 winner 결정).

### 6.6 Output schema 확장

`artifacts/proposals.json`:
```json
{
  "proposals": [
    {"provider": "claude/default", "plan_text": "...", "error": null},
    {"provider": "cursor/auto", "plan_text": "...", "error": null}
  ]
}
```

`artifacts/plan_selector.json`:
```json
{
  "winner_index": 0,
  "winner_provider": "claude/default",
  "reason": "more concrete steps + benchmark threshold called out",
  "scores": [
    {"provider": "claude/default", "structural": 0.82, "llm": 0.91, "final": 0.876},
    {"provider": "cursor/auto",   "structural": 0.74, "llm": 0.70, "final": 0.716}
  ]
}
```

`plan.md` 자체는 winner 의 텍스트 그대로 (downstream — implement / verify — 변경 없음).

### 6.7 Failure modes (워커 C 구현 시 참조)

- 한 strategy 실패 → 나머지로 select.
- 모두 실패 → single fallback (cfg.models.plan 1 회).
- selector LLM 실패 → structural score only.
- proposals 가 1 개 → 그대로 winner (selector 호출 X, 비용 절약).

### 6.8 단위 테스트 (워커 C 가 작성)

`tests/test_strategy_engine.py` (8+):
- 3 proposals fan-out + select.
- Partial failure (1 strategy fail).
- Selector LLM 실패 시 structural fallback.
- 단일 proposal short-circuit (selector 호출 0).
- Tie-break (weight 큰 spec 승리).

`tests/test_workers_multistrategy.py` (3+):
- run_plan single 모드 보존.
- run_plan multi 모드 + winner 가 plan.md 에 들어감.
- proposals.json / plan_selector.json 둘 다 작성.

### 6.9 코드 hooks (워커 C 가 알아야 할 것)

- `JudgeEngine` 의 `_call_one` 패턴이 그대로 재사용 가능 (Config 복제 후 model 만
  override → call_model 호출). 워커 C 는 `judge_engine.py` 를 import 하지 말고 같은
  패턴으로 strategy_engine 안에 복제하는 게 깨끗 (engine 간 결합 회피).
- ThreadPoolExecutor 헬퍼는 두 엔진이 거의 동일하므로, 워커 C 가 작업하면서 공통
  부분이 보이면 `src/agent_loop/_fanout.py` 로 빼낼지 판단 (premature 면 보류).
- prompts/plan.md 는 변경 없음 (동일 prompt 를 N strategies 에 그대로).
- workers.run_plan 은 기존 시그니처 유지 (`(TaskDir, Config) -> ModelResponse`).
  multi 모드에서는 winner 의 ModelResponse 가 반환되도록 — orchestrator 의 cost
  집계가 깨지지 않도록 sum 으로 보강 가능.

## 7. Config schema 변경 종합 (양 트랙 합본)

```python
# config.py
class JudgeSpec(BaseModel):
    provider: str
    weight: float = 1.0

class StrategySpec(BaseModel):     # 워커 C
    provider: str
    weight: float = 1.0

class Runtime(BaseModel):
    sandbox: bool = True
    max_cycles: int = 10
    max_redo: int = 3
    judges: list[JudgeSpec] | None = None
    strategies: list[StrategySpec] | None = None    # 워커 C
```

`_normalize_judges()` / `_normalize_strategies()` 헬퍼가 str list → Spec list 변환.
환경 변수: `AGENT_LOOP_RUNTIME_JUDGES`, `AGENT_LOOP_RUNTIME_STRATEGIES`.

## 8. Output schema 변경 종합

| 파일 | 변경 |
|---|---|
| `judge_result.json` | `consensus: {n_judges, votes_action, votes_better, individual[]}` 추가 (multi 모드). |
| `proposals.json` (NEW) | 워커 C. multi-strategy 모드에서만 작성. |
| `plan_selector.json` (NEW) | 워커 C. selector 결정 audit. |
| `plan.md` | 변경 없음 (winner text 그대로). |
| `metrics.jsonl` | judge phase 행에 `n_judges` / `votes_action` 옵션 키. |

## 9. Backward compat

- v0.2 task 디렉토리 그대로 resume 가능 — `runtime.judges` 기본 None 이므로 single
  모드로 동작.
- judge_result.json 에 `consensus` 키가 없으면 single 모드 — orchestrator 가 그대로
  처리.
- 기존 prompts/judge.md, prompts/plan.md 변경 없음.
- config.toml 에 새 키가 없으면 무해.

## 10. 작업 분해 (워커 B 본 PR)

| # | 작업 | 산출물 |
|---|---|---|
| 1 | `docs/plan-v0.3.md` (이 파일) | this |
| 2 | `config.py` 에 `JudgeSpec` + `Runtime.judges` + `_normalize_judges` + ENV override | Config 확장 |
| 3 | `judge_engine.py` 신규 (~150줄) | JudgeEngine + dataclasses + result_to_dict |
| 4 | `workers.py:run_judge` 분기 + `_run_judge_single` 추출 | backward compat 유지 |
| 5 | `orchestrator.py` 미세 — metrics.jsonl 의 judge 행에 n_judges 추가 | metric 1 키 추가 |
| 6 | `cli.py` `--judge` 플래그 (run / bench), config show 표시 | 사용자 표면 |
| 7 | `tests/test_judge_engine.py` (10) + `tests/test_workers_multijudge.py` (3) | 13 tests |
| 8 | `README.md` Multi-judge (v0.3) 섹션, `docs/architecture.md` Judge Engine 박스 | doc |
| 9 | `progress.txt` 갱신 | log |

## 11. 성공 기준

1. `pytest -q` 가 v0.2.1 기존 77 + 신규 13+ 모두 green (regression 0).
2. mock 3-judge consensus 시나리오 (claude/gemini/cursor) 가 stop 으로 합의 → action,
   votes_action, weighted score 모두 정확.
3. partial failure (1 judge 실패) 가 partial consensus 로 끝남, judge_result.json 에
   `consensus.individual[*].error` 가 빠진 행 정확히 1.
4. all-fail 시 single fallback 으로 동작 (judge_result.json 의 `consensus.fallback=true` +
   reason 명시), 사이클은 중단되지 않음.
5. backward compat: `runtime.judges` None 인 task 가 v0.2 e2e 와 1바이트도 다르지 않은
   judge_result.json 을 만든다.
6. CLI `--judge claude/default --judge gemini/gemini-2.5-flash` 가 config 의 judges 를
   덮어씀.

## 12. 위험 / 한계

| 위험 | 대응 |
|---|---|
| ThreadPool 동시 CLI subprocess 가 inode quota / load 에 부담 | judges 권장 ≤ 3. doctor 에 quota check 추가는 v0.4. |
| 같은 vendor 중복 spawn → 의견 다양성 0 | README 에 명시 + e2e 권장 config 는 cross-vendor. |
| Judge 응답 latency 의 max() 가 single 보다 느릴 수 있음 | gemini-flash 권장 (~8s), 가장 느린 spec 이 critical path. |
| consensus 알고리즘이 단순 weighted majority — 복잡한 의견 차이 미수용 | v0.3.1 검토. 일단 단순 합의로 시작 (KISS). |
| Selector LLM 실패가 strategy 흐름 막을 수 있음 | structural fallback 으로 항상 진행. (워커 C 책임.) |

## 13. 다음 단계 (이 plan 후)

1. (워커 B = 이 PR) 작업 #2~#9 완료 → progress.txt 갱신, push X.
2. (워커 C) 이 문서 5절을 참조해 strategy_engine.py + run_plan 분기 + tests 구현.
3. (v0.3.1) cross-vendor 라이브 검증 — claude/default + gemini/gemini-2.5-flash + cursor/auto
   3-vendor 합의가 단일 cursor/auto 보다 안정적인지 측정. 필요 시 weight 자동 조정 (자기
   모순 / regression 빈도 기반).
4. (v0.4) Selector 자체를 multi-judge 로 교체 — 두 엔진이 자연스럽게 fan-out 추상으로
   수렴할 가능성 검토.

## 14. 결정 사항 (확정)

- `[[judges]]` TOML 표기 + `[runtime].judges` 단순 리스트 둘 다 허용.
- 기본 `weight = 1.0`. 음수 / 0 가중치는 validation 에러.
- partial failure 는 silent (warning console 한 줄), all-fail 만 single fallback.
- consensus tie-break: action 은 `stop` 우선, better 는 `False` 보수.
- Judge prompt 는 동일 (provider 만 다름). v0.4 에 차별 prompt 검토.
- multi-strategy 의 Selector 는 v0.3.0 에서 휴리스틱+LLM 1회 (multi-judge 재사용 X).
- `workers.run_judge` 의 first-cycle short-circuit 은 multi 모드에서도 적용 (best
  없으면 single 위임 → ThreadPool 비용 0).
