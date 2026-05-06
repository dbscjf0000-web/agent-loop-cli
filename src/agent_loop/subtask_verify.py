"""Step C — Sub-task verifier dispatcher.

P단계가 plan.md에 sub-task 목록을 작성하면, V단계가 각 sub-task의
``verifier`` 종류(``pytest`` / ``rule`` / ``llm_rubric``)에 따라 적절한
검증 도구로 실행한다.

`run_subtask_verifications()`가 진입점. 결과는 도메인 무관 dict 리스트
(`name`, `verifier`, `passed`, `detail`)로 반환된다. V phase가 이를
``solution.json``에 별도 섹션으로 기록한다.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# plan.md sub-task parser
# ---------------------------------------------------------------------------
_HEAD_RE = re.compile(r"^###\s+(subtask-[A-Za-z0-9_-]+):\s*(.+?)\s*$")
_FIELD_RE = re.compile(r"^-\s+(goal|acceptance|verifier|check_hint|depends_on)\s*:\s*(.*)$")


@dataclass
class Subtask:
    id: str
    name: str
    goal: str = ""
    acceptance: str = ""
    verifier: str = ""
    check_hint: str = ""
    depends_on: str = ""


def parse_subtasks(plan_md: str) -> list[Subtask]:
    """Parse the ``## 3. Sub-tasks`` section of plan.md into ``Subtask`` records.

    Robust to surrounding sections — it just walks lines top-to-bottom.
    Multi-line `acceptance` blocks (until next field/header) are concatenated.
    """
    subtasks: list[Subtask] = []
    current: Subtask | None = None
    current_field: str | None = None
    for line in (plan_md or "").splitlines():
        head = _HEAD_RE.match(line)
        if head:
            if current is not None:
                subtasks.append(current)
            current = Subtask(id=head.group(1), name=head.group(2))
            current_field = None
            continue
        if current is None:
            continue
        if line.startswith("##") and not line.startswith("###"):
            # left the sub-tasks section
            subtasks.append(current)
            current = None
            current_field = None
            continue
        m = _FIELD_RE.match(line)
        if m:
            current_field = m.group(1)
            setattr(current, current_field, m.group(2).strip())
            continue
        # continuation line for last field (e.g. multi-line acceptance)
        if current_field and line.strip() and not line.startswith("- "):
            existing = getattr(current, current_field, "")
            joined = (existing + "\n" + line.strip()).strip()
            setattr(current, current_field, joined)
    if current is not None:
        subtasks.append(current)
    return subtasks


# ---------------------------------------------------------------------------
# verifier dispatch
# ---------------------------------------------------------------------------
@dataclass
class SubtaskResult:
    id: str
    name: str
    verifier: str
    passed: bool
    detail: str = ""


def run_subtask_verifications(
    plan_md: str,
    workspace: Path,
    *,
    pytest_timeout: int = 60,
) -> list[SubtaskResult]:
    """Dispatch each sub-task to the appropriate verifier and return results.

    Skips sub-tasks with no/unknown verifier (records as ``passed=False`` with
    ``detail='no verifier'``). Never raises — verifier crashes are captured.
    """
    out: list[SubtaskResult] = []
    for st in parse_subtasks(plan_md):
        v = (st.verifier or "").strip().lower()
        try:
            if v == "pytest":
                out.append(_verify_pytest(st, workspace, pytest_timeout))
            elif v == "rule":
                out.append(_verify_rule(st, workspace))
            elif v == "llm_rubric":
                out.append(
                    SubtaskResult(
                        id=st.id, name=st.name, verifier="llm_rubric",
                        passed=True, detail="llm_rubric: deferred to legacy V (axis-level)",
                    )
                )
            else:
                out.append(
                    SubtaskResult(
                        id=st.id, name=st.name, verifier=v or "(none)",
                        passed=False, detail="unknown or missing verifier",
                    )
                )
        except Exception as e:
            out.append(
                SubtaskResult(
                    id=st.id, name=st.name, verifier=v,
                    passed=False, detail=f"verifier crashed: {type(e).__name__}: {e}",
                )
            )
    return out


# ---------------------------------------------------------------------------
# pytest verifier — runs test_<id>.py if present
# ---------------------------------------------------------------------------
def _verify_pytest(st: Subtask, workspace: Path, timeout: int) -> SubtaskResult:
    """Run ``test_<id>.py`` in workspace via pytest. Falls back to any
    ``test_*.py`` if the per-id file is absent."""
    # Map subtask-1 → test_subtask1.py
    sid_token = st.id.replace("subtask-", "subtask")
    candidates = [
        workspace / f"test_{sid_token}.py",
        workspace / f"test_{st.id.replace('-', '_')}.py",
    ]
    target = next((c for c in candidates if c.exists()), None)
    if target is None:
        return SubtaskResult(
            id=st.id, name=st.name, verifier="pytest",
            passed=False, detail=f"missing test file (looked for {[c.name for c in candidates]})",
        )
    import sys as _sys
    # Codex review fix: prefer the same Python that's running us so the
    # subprocess inherits installed packages (pytest, numpy, etc.) instead
    # of falling back to a stale system Python with empty output on import error.
    try:
        # Pass just the filename — cwd is already the workspace, and using a
        # full relative path causes "file not found" when workspace itself is
        # given as a relative path (subprocess resolves cwd against caller's
        # cwd, but argv strings are not re-resolved by pytest).
        proc = subprocess.run(
            [_sys.executable, "-m", "pytest", target.name, "-q", "--no-header", "--disable-warnings"],
            cwd=str(workspace.resolve()), capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return SubtaskResult(
            id=st.id, name=st.name, verifier="pytest",
            passed=False, detail=f"pytest timeout > {timeout}s",
        )
    except FileNotFoundError as e:
        return SubtaskResult(
            id=st.id, name=st.name, verifier="pytest",
            passed=False, detail=f"pytest binary not found: {e}",
        )
    passed = proc.returncode == 0
    # Always include returncode + last lines of stdout/stderr for diagnosability
    out_tail = (proc.stdout or "").strip().splitlines()[-3:]
    err_tail = (proc.stderr or "").strip().splitlines()[-3:]
    bits: list[str] = [f"rc={proc.returncode}"]
    if out_tail:
        bits.append("stdout: " + " | ".join(out_tail))
    if err_tail:
        bits.append("stderr: " + " | ".join(err_tail))
    return SubtaskResult(
        id=st.id, name=st.name, verifier="pytest",
        passed=passed, detail=" || ".join(bits)[:300],
    )


# ---------------------------------------------------------------------------
# rule verifier — text / regex / section / json checks
# ---------------------------------------------------------------------------
_RULE_TEXT_RE = re.compile(r"^text\s*=\s*\"([^\"]+)\"\s*(?:in\s+(\S+))?\s*$")
_RULE_REGEX_RE = re.compile(r"^regex\s*=\s*/([^/]+)/\s*(?:in\s+(\S+))?\s*$")
_RULE_SECTION_RE = re.compile(r"^section\s*=\s*\"([^\"]+)\"\s*(?:in\s+(\S+))?\s*$")
_RULE_JSON_RE = re.compile(r"^json\s*=\s*\"([^\"]+)\"\s*(?:in\s+(\S+))?\s*$")


def _verify_rule(st: Subtask, workspace: Path) -> SubtaskResult:
    """Parse simple rule clauses out of ``check_hint`` and apply each.

    Supported clauses (comma- or newline-separated):
      - ``text="needle" in <file>``         literal substring match
      - ``regex=/pattern/ in <file>``       regex search
      - ``section="## Header" in <file>``   markdown header presence
      - ``json="path.to.key" in <file>``    json key existence

    All clauses must pass for ``passed=True``. Defaults file lookup to
    ``solution.py``, ``solution.md``, ``output.md``, or ``output.json``
    based on the clause type.
    """
    hint = st.check_hint or ""
    if not hint:
        return SubtaskResult(
            id=st.id, name=st.name, verifier="rule",
            passed=False, detail="empty check_hint — nothing to verify",
        )
    # Split clauses on newlines or top-level commas. Commas inside quoted
    # strings are preserved (Codex review fix #4: prevent prefix match
    # silently passing later clauses).
    clauses = _split_rule_clauses(hint)
    failures: list[str] = []
    checked = 0
    unrecognized: list[str] = []
    for clause in clauses:
        m = _RULE_TEXT_RE.match(clause)
        if m:
            checked += 1
            needle, fname = m.group(1), m.group(2) or "solution.py"
            ok, why = _check_text(workspace, fname, needle)
            if not ok:
                failures.append(f"text:{why}")
            continue
        m = _RULE_REGEX_RE.match(clause)
        if m:
            checked += 1
            pattern, fname = m.group(1), m.group(2) or "solution.py"
            ok, why = _check_regex(workspace, fname, pattern)
            if not ok:
                failures.append(f"regex:{why}")
            continue
        m = _RULE_SECTION_RE.match(clause)
        if m:
            checked += 1
            section, fname = m.group(1), m.group(2) or "output.md"
            ok, why = _check_section(workspace, fname, section)
            if not ok:
                failures.append(f"section:{why}")
            continue
        m = _RULE_JSON_RE.match(clause)
        if m:
            checked += 1
            keypath, fname = m.group(1), m.group(2) or "output.json"
            ok, why = _check_json(workspace, fname, keypath)
            if not ok:
                failures.append(f"json:{why}")
            continue
        # Codex review fix #4: unrecognized clauses now FAIL loudly so a
        # malformed rule never passes silently. This catches typos like
        # `len ≤ 250자` that the parser cannot interpret.
        unrecognized.append(clause[:60])
    if checked == 0:
        return SubtaskResult(
            id=st.id, name=st.name, verifier="rule",
            passed=False, detail=f"no recognized rule clauses in check_hint",
        )
    if unrecognized:
        failures.append(f"unrecognized={unrecognized}")
    return SubtaskResult(
        id=st.id, name=st.name, verifier="rule",
        passed=not failures,
        detail="; ".join(failures) if failures else f"{checked} rules passed",
    )


def _split_rule_clauses(text: str) -> list[str]:
    """Split on newlines and top-level commas, preserving commas inside
    double-quoted strings or `/regex/` slashes."""
    clauses: list[str] = []
    buf = ""
    in_str = False
    in_re = False
    for ch in text:
        if ch == '"' and not in_re:
            in_str = not in_str
            buf += ch
        elif ch == "/" and not in_str:
            in_re = not in_re
            buf += ch
        elif ch in (",", "\n") and not in_str and not in_re:
            piece = buf.strip()
            if piece:
                clauses.append(piece)
            buf = ""
        else:
            buf += ch
    piece = buf.strip()
    if piece:
        clauses.append(piece)
    return clauses


def _read_ws_text(workspace: Path, fname: str) -> str | None:
    p = workspace / fname
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return None


def _check_text(workspace: Path, fname: str, needle: str) -> tuple[bool, str]:
    text = _read_ws_text(workspace, fname)
    if text is None:
        return False, f"{fname} not found"
    if needle not in text:
        return False, f'"{needle}" not in {fname}'
    return True, "ok"


def _check_regex(workspace: Path, fname: str, pattern: str) -> tuple[bool, str]:
    text = _read_ws_text(workspace, fname)
    if text is None:
        return False, f"{fname} not found"
    try:
        if not re.search(pattern, text):
            return False, f"/{pattern}/ no match in {fname}"
    except re.error as e:
        return False, f"bad regex: {e}"
    return True, "ok"


def _check_section(workspace: Path, fname: str, header: str) -> tuple[bool, str]:
    text = _read_ws_text(workspace, fname)
    if text is None:
        return False, f"{fname} not found"
    # markdown header — any level
    norm = header.lstrip("# ").strip()
    pattern = re.compile(rf"^#+\s+{re.escape(norm)}\s*$", re.MULTILINE)
    if not pattern.search(text):
        return False, f'section "{norm}" missing in {fname}'
    return True, "ok"


def _check_json(workspace: Path, fname: str, keypath: str) -> tuple[bool, str]:
    text = _read_ws_text(workspace, fname)
    if text is None:
        return False, f"{fname} not found"
    try:
        obj: Any = json.loads(text)
    except json.JSONDecodeError as e:
        return False, f"{fname} not valid json: {e}"
    cur = obj
    for part in keypath.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False, f"key path {keypath} missing at .{part}"
    return True, "ok"


def result_to_dict(r: SubtaskResult) -> dict[str, Any]:
    return asdict(r)


__all__ = [
    "Subtask", "SubtaskResult",
    "parse_subtasks", "run_subtask_verifications", "result_to_dict",
]
