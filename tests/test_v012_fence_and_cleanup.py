"""v0.12.0 follow-up — alt fences (~~~/``) + workspace stale cleanup."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_loop.workers import _extract_workspace_files


# ---------------------------------------------------------------------------
# alt fence delimiters
# ---------------------------------------------------------------------------
def test_tilde_fence_recognized() -> None:
    """~~~ outer fence lets the file body include ``` without truncation."""
    src = (
        "~~~markdown\n"
        "# file: README.md\n"
        "## Install\n"
        "```bash\n"
        "pip install foo\n"
        "```\n"
        "## License\n"
        "MIT\n"
        "~~~\n"
    )
    files = _extract_workspace_files(src)
    assert "README.md" in files
    assert "## License" in files["README.md"]
    assert "MIT" in files["README.md"]
    # Inner ``` survived intact
    assert "```bash" in files["README.md"]


def test_four_backtick_fence_recognized() -> None:
    src = (
        "````markdown\n"
        "# file: README.md\n"
        "## Install\n"
        "```bash\n"
        "pip install bar\n"
        "```\n"
        "## License\n"
        "MIT\n"
        "````\n"
    )
    files = _extract_workspace_files(src)
    assert "README.md" in files
    assert "## License" in files["README.md"]
    assert "pip install bar" in files["README.md"]


def test_three_backtick_still_works_for_code() -> None:
    src = (
        "```python\n"
        "# file: solution.py\n"
        "def f(): pass\n"
        "```\n"
    )
    files = _extract_workspace_files(src)
    assert "solution.py" in files


def test_mismatched_fences_pair_independently() -> None:
    """Open ``` cannot be closed by ~~~ and vice versa — same-delimiter only."""
    src = (
        "```markdown\n"
        "# file: a.md\n"
        "x\n"
        "~~~\n"  # not a closer for ```
        "```\n"
    )
    files = _extract_workspace_files(src)
    assert "a.md" in files
    assert "~~~" in files["a.md"]


def test_three_backtick_breaks_on_inner_three_backtick_legacy() -> None:
    """Documents the known limitation: 3-backtick outer + 3-backtick inner
    truncates. Justifies why the prompt advises 4-backtick / tilde."""
    src = (
        "```markdown\n"
        "# file: README.md\n"
        "## Install\n"
        "```bash\n"
        "pip install foo\n"
        "```\n"
        "## License\n"
        "MIT\n"
        "```\n"
    )
    files = _extract_workspace_files(src)
    assert "README.md" in files
    # README is truncated at the first inner ``` — License/MIT lost.
    # This is the bug nested-fence prompt guidance avoids.
    assert "## License" not in files["README.md"]


# ---------------------------------------------------------------------------
# workspace stale cleanup
# ---------------------------------------------------------------------------
def test_run_implement_clears_old_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stale files from a previous I phase must be cleaned, except
    best_solution.* which the orchestrator owns for rollback."""
    monkeypatch.chdir(tmp_path)
    from agent_loop.config import Config
    from agent_loop.state import TaskDir, new_task_id

    td = TaskDir(root=tmp_path / ".agent_loop", task_id=new_task_id())
    td.init()
    ws = td.workspace_path()

    # Pre-populate workspace with mixed leftovers.
    (ws / "stale_solution.py").write_text("# old", encoding="utf-8")
    (ws / "old_manuscript.md").write_text("# old draft", encoding="utf-8")
    (ws / "test_subtask1.py").write_text("def test_x(): pass", encoding="utf-8")
    (ws / "best_solution.py").write_text("# preserved", encoding="utf-8")
    (ws / "best_solution.json").write_text('{"x":1}', encoding="utf-8")
    (ws / "__pycache__").mkdir()
    (ws / "__pycache__" / "x.pyc").write_text("", encoding="utf-8")

    # Stub call_model so run_implement just runs the cleanup + write path.
    captured = {}
    class _R:
        text = "```python\n# file: solution.py\ndef f(): pass\n```\n"
        prompt_tokens = 0
        completion_tokens = 0
        cost_usd = 0.0
        latency_s = 0.0
        model = "(fake)"
    def fake_call(phase, prompt, *, system=None, config=None, workspace=None):
        captured["called"] = True
        return _R()
    # Patch the symbol as imported into workers (not the source module),
    # because workers.py did `from agent_loop.models import call_model`
    # at import time.
    monkeypatch.setattr("agent_loop.workers.call_model", fake_call)

    from agent_loop.workers import run_implement
    # task.md + plan.md need to exist for run_implement to read them.
    td.task_md_path().write_text("dummy task", encoding="utf-8")
    td.write_artifact("plan.md", "# Plan\n## 1. 산출물\n- workspace/solution.py\n")

    run_implement(td, Config())

    files = {p.name for p in ws.iterdir() if p.is_file()}
    # Cleaned away
    assert "stale_solution.py" not in files
    assert "old_manuscript.md" not in files
    # Test files: cleaned by the existing test_subtask*.py rule
    assert "test_subtask1.py" not in files
    # Preserved
    assert "best_solution.py" in files
    assert "best_solution.json" in files
    # New file from this run
    assert "solution.py" in files
    # __pycache__ kept as a directory
    assert (ws / "__pycache__").is_dir()
