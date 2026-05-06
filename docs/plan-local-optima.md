# Plan: Local Optima 탈출 전략

작성일: 2026-05-06
출처: Claude / Codex / Gemini 3자 토의 누적

---

## 1. 문제 정의

```
┌──────────────────────────────────────────┐
│  실증된 정체 (progress.txt 기록)           │
│                                            │
│   palindrome  cycles=1,2,3 → 0.70 동점    │
│   sort_tuning cycles=1,2,3 → 0.60 동점    │
│   시간만 3~12배 증가                       │
│                                            │
│  근본 원인:                                │
│   Judge hint = "perf 다시" (의미적)         │
│         ↓ 부족                            │
│   필요 = "Manacher 교체" (구조적)           │
└──────────────────────────────────────────┘
```

## 2. 누적된 시도와 효과

```
┌──────────────────────────────────────────────────┐
│  v0.3 ✅  Vendor 병렬 (Vertical)                  │
│           효과: vendor 합의 신뢰도 ↑                │
│           한계: 같은 family에 수렴                  │
│                                                    │
│  v0.6 ✅  구조적 Judge hint                        │
│           효과: cycle 간 algorithm 변화              │
│           한계: judge가 family 못 떠올릴 때 정체       │
│                                                    │
│  v0.7.2 ✅ Prior context block                     │
│           효과: cycle 1=0.60 → cycle 3=0.94         │
│           한계: 단일 trajectory                      │
└──────────────────────────────────────────────────┘
```

## 3. 토의 결과 — 후보 해법

### 3.1 Wiki / TDD 도입 평가

| 도구           | Codex 진단                        | Gemini 진단                   |
| ------------ | ------------------------------- | --------------------------- |
| **LLM Wiki** | "그냥 요약이면 무효, 연결 있어야"             | Tabu Search: 실패 경로 누적, 중복 회피 |
| **Red gate** | "탐색기 X, 안전장치"                    | Pivot 강제: 모호 hint를 정량 실패로 치환 |
| **본질 해법**    | family-pivot policy + multi-strategy | Wiki+TDD 자체가 메커니즘             |

**합의**: Wiki/TDD는 **본질 해법이 아닌 보조축**. 단독으론 부족, 함께 쓰면 효과 큼.

### 3.2 병렬 전략 평가

```
[ v0.3 — Vertical 병렬 ]                  [ 제안 — Horizontal (Island Model) ]

  cycle 1 ──┬── cursor                     Island A: Manacher family
            ├── gemini  ──┐                Island B: KMP family
            └── claude   ──┘ judge 합의      Island C: DP family
  (모두 같은 prior 기반)                       (각 island 독립 진화 + migration)
  
  → vendor diversity ≠ algorithm diversity   → algorithm-family 다양성 확보
```

| 항목         | Codex                       | Gemini                          |
| ---------- | --------------------------- | ------------------------------- |
| **v0.3 진단** | "한 cycle 내부 탐색" 한계 도달          | "Ensemble = 합의 집중, 다양성 부족"        |
| **권장 구조**   | cycle-level branch           | Multi-Trajectory / Island Model |
| **분기 기준**   | algorithm family 강제           | 알고리즘 설계(재귀/반복/SIMD)              |

## 4. 통합 진단

```
   현재 약점 = "병렬은 했지만 다양성이 vendor 단위였다"
                            ↓
   필요한 것 = "병렬 + 다양성을 algorithm-family 단위로"
                            ↓
   본질 해법 후보:
                                                    
   1순위: Island Model (Codex+Gemini 합의)          
          └─ family 분기 + state isolation         
                                                    
   2순위: Family-pivot policy                       
          └─ 단일 trajectory에서 family 강제 변경   
                                                    
   3순위: Wiki + TDD                                
          └─ 위 둘을 더 풍부하게 만드는 보조축        
```

## 5. 효과 vs 비용

```
        효과 ↑
         │
  ★★★ Island Model (구조 변경 큼, 효과 큼)
         │
  ★★  Family-pivot policy (기존 구조 유지, 효과 중간)
         │
  ★    Wiki/TDD (보조축, 위 둘과 결합 시 효과)
         │
         └─────────────── 구현 복잡도 →
```

## 6. 추천 진행 순서

```
┌─────────────────────────────────────────────┐
│  Phase B: Family-pivot policy (작은 변경)     │
│   ├─ Judge에 "같은 family 2회 실패시 강제 pivot"│
│   ├─ family 분류 휴리스틱                      │
│   └─ 위험: 작음, 기존 구조 유지                  │
│                                               │
│  Phase C: Wiki + TDD (보조)                   │
│   ├─ Wiki: 실패/성공 family 누적                │
│   ├─ TDD regression: pivot 후 안전망            │
│   └─ Plan/Judge에 prior 주입                    │
│                                               │
│  Phase A: Island Model (큰 변경, 마지막)        │
│   ├─ N개 island 병렬 진화                      │
│   ├─ 주기적 migration (elite swap)              │
│   └─ 위험: state isolation, 평가비용 큼          │
└─────────────────────────────────────────────┘

순서 이유: B → C → A (점진적, 위험 최소)
```

## 7. 미해결 위험

| 위험            | 완화 후보                  |
| ------------- | ---------------------- |
| Family 자동 분류 정확도 | 기본 휴리스틱 + LLM 분류 fallback |
| Branch collapse | diversity penalty       |
| 평가 비용 폭증       | island 수 제한 + early kill |
| Migration 정책 불명확 | "elite swap + restart" 단순안 |

## 8. 다음 작업

- 이 plan과 별개로, **루프 전체 약점 점검**을 추가 토의 진행 중 (Claude/Codex/Gemini)
- 합의 후 plan-loop-weaknesses.md에 별도 저장 예정
