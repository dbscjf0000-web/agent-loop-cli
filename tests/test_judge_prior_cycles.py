"""v0.6 Judge prompt enhancement: prior_cycles injection.

Covers:
  - ``_collect_prior_cycles_summary`` returns "" on cycle 1 (no history yet).
  - It picks up verify scores + judge hints from history.jsonl across cycles.
  - It injects axes summary from solution.json + a code excerpt.
  - ``max_chars`` truncation drops older cycles first.
  - ``_run_judge_single`` actually passes the rendered string into the prompt
    (verified via call_model patching — no real LLM).

All tests mock the LLM. No live calls.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_loop import workers
from agent_loop.config import Config
from agent_loop.state import TaskDir


def _write_history(td: TaskDir, rows: list[dict[str, Any]]) -> None:
    """Append JSON rows to memory/history.jsonl (one per line)."""
    td.memory_dir().mkdir(parents=True, exist_ok=True)
    path = td.memory_dir() / "history.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _fake_completion(text: str, pt: int = 5, ct: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=pt, completion_tokens=ct),
    )


# ---------- _collect_prior_cycles_summary ----------


def test_collect_prior_cycles_empty_when_no_history(tmp_path: Path) -> None:
    td = TaskDir(root=tmp_path, task_id="abc111")
    td.init()
    # No history.jsonl entries yet.
    assert workers._collect_prior_cycles_summary(td) == ""


def test_collect_prior_cycles_reads_two_cycles(tmp_path: Path) -> None:
    td = TaskDir(root=tmp_path, task_id="def222")
    td.init()
    _write_history(
        td,
        [
            {"cycle": 1, "phase": "verify", "score": 0.70,
             "summary": "perf=0.0 correctness=1.0"},
            {"cycle": 1, "phase": "judge", "summary": "perf weak",
             "hint": "perf axis again"},
            {"cycle": 2, "phase": "verify", "score": 0.70,
             "summary": "perf=0.0 again"},
            {"cycle": 2, "phase": "judge", "summary": "perf still weak",
             "hint": "try Manacher's algorithm O(n)"},
        ],
    )
    out = workers._collect_prior_cycles_summary(td)
    assert "## Prior cycles" in out
    assert "Cycle 1" in out
    assert "Cycle 2" in out
    # Both hints surfaced verbatim so the judge can detect repetition.
    assert "perf axis again" in out
    assert "Manacher" in out
    # weighted_score formatted to 2 decimals.
    assert "weighted_score=0.70" in out


def test_collect_prior_cycles_includes_axes_and_code_excerpt(tmp_path: Path) -> None:
    td = TaskDir(root=tmp_path, task_id="ghi333")
    td.init()
    _write_history(
        td,
        [
            {"cycle": 1, "phase": "verify", "score": 0.42,
             "summary": "summary 1"},
        ],
    )
    # Latest axes come from solution.json (canonical v0.2 schema).
    td.write_artifact(
        "solution.json",
        {
            "weighted_score": 0.42,
            "axes": [
                {"name": "correctness", "score": 0.8, "weight": 0.5},
                {"name": "perf", "score": 0.0, "weight": 0.5},
            ],
            "summary": "stuck on perf",
        },
    )
    # Last attempted code excerpt.
    sol_py = td.workspace_path() / "solution.py"
    sol_py.parent.mkdir(parents=True, exist_ok=True)
    sol_py.write_text(
        "def find_palindrome(s):\n    # expand-around-center O(n^2)\n"
        "    return s[::-1]\n",
        encoding="utf-8",
    )

    out = workers._collect_prior_cycles_summary(td)
    assert "Latest axes" in out
    assert "correctness:0.80" in out
    assert "perf:0.00" in out
    assert "expand-around-center" in out  # code excerpt landed
    assert "```python" in out


def test_collect_prior_cycles_truncates_oldest_first(tmp_path: Path) -> None:
    td = TaskDir(root=tmp_path, task_id="jkl444")
    td.init()
    rows: list[dict[str, Any]] = []
    # Pad each cycle with a long hint so the rendered text grows past max_chars.
    long_hint = "X" * 180  # leaves room for the bullet wrapper text
    for cyc in range(1, 6):  # 5 cycles
        rows.append(
            {"cycle": cyc, "phase": "verify", "score": 0.5,
             "summary": f"summary cycle {cyc}"}
        )
        rows.append(
            {"cycle": cyc, "phase": "judge", "summary": "weak",
             "hint": f"{cyc}:{long_hint}"}
        )
    _write_history(td, rows)
    out = workers._collect_prior_cycles_summary(td, max_chars=600)
    assert len(out) <= 600
    # Most recent cycle must always survive the truncation.
    assert "Cycle 5" in out
    # Oldest cycle should be dropped.
    assert "Cycle 1" not in out


# ---------- _run_judge_single carries prior_cycles into the prompt ----------


def test_run_judge_single_passes_prior_cycles_to_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prompt rendered for the LLM must contain the prior_cycles section."""
    td = TaskDir(root=tmp_path, task_id="mno555")
    td.init()
    td.write_artifact("solution.json", {"weighted_score": 0.6, "axes": []})
    td.write_artifact("best_solution.json", {"weighted_score": 0.5})
    _write_history(
        td,
        [
            {"cycle": 1, "phase": "verify", "score": 0.5,
             "summary": "init verify"},
            {"cycle": 1, "phase": "judge", "summary": "first cycle",
             "hint": "tighten loops"},
        ],
    )

    captured: dict[str, Any] = {}

    def fake_call_model(phase: str, prompt: str, **kwargs: Any) -> Any:
        captured["phase"] = phase
        captured["prompt"] = prompt
        from agent_loop.models import ModelResponse
        return ModelResponse(
            text=json.dumps(
                {
                    "better": True,
                    "action": "stop",
                    "reason": "ok",
                    "hint": "",
                    "scores": {"this_cycle": 0.6, "best": 0.5, "delta": 0.1},
                }
            ),
            prompt_tokens=10,
            completion_tokens=10,
            cost_usd=0.0,
            latency_s=0.01,
            model="mock",
        )

    monkeypatch.setattr(workers, "call_model", fake_call_model)
    workers.run_judge(td, Config())

    assert captured.get("phase") == "judge"
    p = captured.get("prompt", "")
    # prior_cycles section should be present and contain the prior hint.
    assert "## Prior cycles" in p
    assert "Cycle 1" in p
    assert "tighten loops" in p
