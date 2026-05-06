# Plan: agent-loop-cli 약점 점검 + 커버 개념

작성일: 2026-05-06
출처: Claude / Codex / Gemini 3자 토의

---

## 1. 약점의 4대 분류 (Codex 정리)

```
┌─────────────────────────────────────────────┐
│  본질 진단 (Codex):                          │
│  "생각을 더 잘하는 문제"보다                   │
│  "분기·검증·상태관리·예산관리" 문제           │
└─────────────────────────────────────────────┘

   ┌──────────────────────┐
   │ 1. 탐색 다양성 부족    │ ← 가장 큰 약점
   ├──────────────────────┤
   │ 2. 평가기 비실행성     │ ← Verify 불안정
   ├──────────────────────┤
   │ 3. 상태/환경 불안정    │ ← infra 문제
   ├──────────────────────┤
   │ 4. 비용/종료 제어 부재  │ ← 자원 낭비
   └──────────────────────┘
```

## 2. 약점 → 커버 개념 통합 매트릭스

| 분류            | 약점                               | 커버 개념·기법                             | 출처             |
| ------------- | -------------------------------- | ------------------------------------ | -------------- |
| **추론/전략 다양성**   | cycle 2~3 local optima 정체            | explicit pivot operator              | Codex          |
|               | Judge hint 의미적, algorithm pivot 불가 | family-pivot policy, hypothesis registry | Codex          |
|               | v0.3 vendor 병렬 다양성 부족              | portfolio + beam search, **Island Model**   | Codex+Gemini |
|               | 매몰 비용 오류 (선형 탐색)                  | **Tree-of-Thought (ToT)**, MCTS              | Gemini       |
|               | 외부 최신 지식 연동 부재                    | Web Search / RAG 통합                    | Gemini       |
|               | novelty 부재                          | novelty/diversity regularizer        | Codex          |
| **검증 신뢰성**     | Wiki/TDD 미도입                       | living wiki/spec memory, TDD         | Codex+Gemini |
|               | flaky verify                       | verifier ensemble, property-based test | Codex+Gemini |
|               | spec drift, judge gaming           | executable spec, counterexample-guided repair | Codex   |
|               | 검증 커버리지 측정 결여                     | Mutation Testing, pytest-cov          | Gemini       |
| **상태/인프라**    | cursor-agent timeout/cold-start    | warm pool, checkpoint-resume         | Codex          |
|               | workspace state isolation         | branch-per-agent + artifact handoff  | Codex          |
|               | ~/.cursor inode quota              | quota GC/lease, content-addressed cache | Codex+Gemini |
|               | tool/env nondeterminism            | pinned env + hermetic sandbox        | Codex          |
|               | 인프라 장애 복구 불가                       | **Checkpointing**, Docker/LXC sandbox     | Gemini       |
| **비용/관측성**    | 컨텍스트 압축 중 의도·근거 손실                  | decision/provenance log              | Codex          |
|               | 관측성 부족                              | trajectory tracing                    | Codex          |
|               | 비용·지연 폭증                           | budgeted search, **dynamic token budgeting** | Codex+Gemini |
|               | 종료 기준 취약, 무한수정                     | stagnation detector, stop-rule + best-so-far commit | Codex |
|               | 환경 오염                               | ephemeral FS                          | Gemini       |

## 3. 새로 발견된 약점 (3자 추가)

```
┌──────────────────────────────────────────┐
│  Codex 추가:                              │
│   • spec drift / judge gaming             │
│   • tool/env nondeterminism                │
│   • 컨텍스트 압축 시 의도·근거 손실           │
│   • 관측성 부족 (trajectory tracing)        │
│   • 비용·지연 폭증                          │
│   • 종료 기준 취약 (무한수정 위험)           │
│                                            │
│  Gemini 추가:                              │
│   • 매몰 비용 오류 (선형 탐색의 본질적 한계)   │
│   • 외부 최신 지식 연동 부재                 │
│   • 검증 커버리지 측정 결여                  │
│   • 인프라 장애 시 체크포인팅 부재             │
│   • 환경 오염                              │
└──────────────────────────────────────────┘
```

## 4. 우선순위 분석 — 효과 × 비용

```
            효과 ↑
             │
   ★★★★ Family-pivot policy        ◀── 작은 변경, 큰 효과
             │
   ★★★  Stagnation detector + stop-rule
             │
   ★★★  Living wiki (decision log)
             │
   ★★    Island Model / ToT          ◀── 큰 변경, 큰 효과
             │
   ★★    TDD regression bank
             │
   ★      Verifier ensemble
             │
   ★      Hermetic sandbox
             │
             └────────────────── 구현 복잡도 →
```

## 5. 단계적 도입 로드맵

```
┌──────────────────────────────────────────────┐
│  Phase 1 — Quick Wins (작은 변경, 큰 효과)      │
│   ├─ Stagnation detector                       │
│   │   같은 점수 N번 → 자동 stop (무한수정 차단)  │
│   ├─ Family-pivot policy                       │
│   │   같은 family 2회 실패 → 강제 변경            │
│   └─ Best-so-far commit                        │
│       정체 시 최고 결과로 종료                    │
│                                                 │
│  Phase 2 — 검증 강화 (중간 변경)                  │
│   ├─ Verifier ensemble (flaky 완화)              │
│   ├─ TDD regression bank                       │
│   └─ Property-based test (몇 개만)               │
│                                                 │
│  Phase 3 — 지식 시스템 (중간 변경)                │
│   ├─ LLM Wiki (failed/successful family 누적)    │
│   ├─ Decision/provenance log                    │
│   └─ Plan/Judge에 prior 주입                      │
│                                                 │
│  Phase 4 — 구조 변경 (큰 변경, 마지막)             │
│   ├─ Island Model (algorithm-family 분기)        │
│   ├─ ToT / MCTS 탐색기                            │
│   └─ Hermetic sandbox                           │
└──────────────────────────────────────────────┘
```

## 6. 즉시 도입 권고 (Phase 1)

```
약점                    │ 커버 기법              │ 구현 비용
────────────────────────┼─────────────────────┼─────────
무한수정 위험             │ Stagnation detector   │ 작음 (judge 후 비교)
같은 family 정체          │ Family-pivot policy    │ 중간 (family 분류기)
매몰 비용 오류             │ Best-so-far commit     │ 작음 (점수 기록만)
─────────────────────────────────────────────────────
효과: cycle 2~3 정체의 70% 커버 추정
```

## 7. 트레이드오프 명시

```
┌──────────────────────────────────────────┐
│  ✅ 도입 시 이득                           │
│   ├─ 자원 낭비 감소 (stop-rule)             │
│   ├─ Local optima 빈도 ↓                  │
│   └─ 디버깅 가능성 ↑ (provenance log)       │
│                                            │
│  ⚠️  도입 시 비용                          │
│   ├─ Phase 4까지 가면 구조 큰 변경           │
│   ├─ family 분류 휴리스틱 정확도 위험         │
│   └─ ToT/MCTS는 평가 비용 폭증 위험          │
└──────────────────────────────────────────┘
```

## 8. 단순성 원칙 점검 결과 (2026-05-06 업데이트)

3자(Claude/Codex/Gemini) 만장일치: **Phase 3/4 도입 시 "가볍고 단순" 원칙이 깨짐**.

### Codex 핵심 통찰
> "복잡성은 루프 엔진이 아니라 검증/입력 쪽에만 머물러야 한다"

### Gemini 핵심 통찰
> "'지능형 루프'가 아닌 '실수를 반복하지 않는 엄격한 도구'로 남기"

### 처분 합의

| Phase   | 항목                       | 처분        |
| ------- | ------------------------ | --------- |
| **1**   | Stagnation detector       | ✅ 유지     |
|         | Family-pivot policy        | ✅ 유지     |
|         | Best-so-far commit         | ✅ 유지     |
| **2**   | TDD regression bank        | ✅ 유지 (소형) |
|         | Verifier ensemble          | ❌ 폐기    |
| **3**   | Decision log (1파일 txt)    | ✅ 유지     |
|         | **LLM Wiki**              | ❌ **폐기** |
| **4**   | Island Model               | ❌ 폐기 (실험 브랜치만) |
|         | ToT / MCTS                 | ❌ 폐기    |

### 최종 도입셋

```
┌────────────────────────────────────────────┐
│  남길 것 (5가지 — 모두 가벼움)               │
│   1. Stagnation detector                    │
│   2. Family-pivot policy                    │
│   3. Best-so-far commit                     │
│   4. TDD regression bank (tests/regression/) │
│   5. Decision log (1파일)                    │
│                                              │
│  목표 효과:                                   │
│   "복잡도 1/10로 효과 90% 달성"                │
└────────────────────────────────────────────┘
```

### 핵심 메시지

```
LLM Wiki는 매력적이지만,
agent-loop-cli의 본질엔 맞지 않음.

   본질: 가볍고 단순한 검증 도구
   Wiki: 무거운 지식 관리 시스템
   → 미스매치 → 폐기

decision.log 1파일 + regression/ 폴더로 충분.
```

## 9. 다음 작업

1. ✅ plan 업데이트 (이 섹션)
2. ⏳ Claude/Codex/Cursor 사용량 동적 라우팅 설정
3. ⏳ Phase 1 (5가지) 실험 — 실제 효과 측정
4. plan-wiki-adoption.md는 **deprecated 표시** 권고
