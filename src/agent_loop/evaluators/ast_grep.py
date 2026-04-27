"""Source-inspection evaluator (text-based pattern grep).

Spec keys:
    weight (float)
    rule   (str) semicolon-separated mini-DSL of source-inspection rules.
    file   (str, optional) workspace-relative file (default "solution.py").

Mini-DSL (one rule per ``;``-separated chunk):
    "<token>_count<=N"     occurrences of <token> in source must be <= N
    "<token>_count>=N"     occurrences must be >= N
    "<token>_count==N"     exactly N
    "<token> not_in"       token must NOT appear
    "<token> in"           token MUST appear at least once

Tokens may be quoted with backticks for things containing special chars:
    "`for `_count<=1; `.index(` not_in"

Score: starts at 1.0, each violation subtracts 0.5 (clipped to 0..1).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from agent_loop.config import Config
from agent_loop.state import TaskDir
from agent_loop.verify_types import AxisScore


_RULE_PATTERNS = (
    re.compile(r"^(?P<token>.+?)_count(?P<op><=|>=|==)(?P<n>\d+)$"),
    re.compile(r"^(?P<token>.+?)\s+(?P<op>not_in|in)$"),
)


def _strip_token(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == "`" and raw[-1] == "`":
        return raw[1:-1]
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        return raw[1:-1]
    return raw


def _check_one(rule: str, source: str) -> tuple[bool, str]:
    """Return (passed, evidence)."""
    rule = rule.strip()
    if not rule:
        return True, "(empty rule)"
    for pat in _RULE_PATTERNS:
        m = pat.match(rule)
        if not m:
            continue
        token = _strip_token(m.group("token"))
        op = m.group("op")
        if op in {"<=", ">=", "=="}:
            n = int(m.group("n"))
            count = source.count(token)
            ok = (
                (op == "<=" and count <= n)
                or (op == ">=" and count >= n)
                or (op == "==" and count == n)
            )
            return ok, f"count({token!r})={count} {op} {n} -> {'pass' if ok else 'fail'}"
        if op == "not_in":
            ok = token not in source
            return ok, f"{token!r} {'absent' if ok else 'present'} (want absent)"
        if op == "in":
            ok = token in source
            return ok, f"{token!r} {'present' if ok else 'absent'} (want present)"
    return False, f"could not parse rule: {rule!r}"


def run_ast_grep(
    *,
    name: str,
    spec: dict[str, Any],
    task_dir: TaskDir,
    config: Config,
) -> AxisScore:
    weight = float(spec.get("weight", 1.0) or 0.0)
    rule = spec.get("rule") or ""
    file_name = str(spec.get("file") or "solution.py")
    target = task_dir.workspace_path() / file_name
    if not target.exists():
        return AxisScore(
            name=name,
            score=0.0,
            weight=weight,
            evaluator="ast_grep",
            evidence=f"{file_name} does not exist",
            is_ground_truth=True,
        )
    source = target.read_text(encoding="utf-8")

    rules = [r for r in str(rule).split(";") if r.strip()] if rule else []
    if not rules:
        return AxisScore(
            name=name,
            score=1.0,
            weight=weight,
            evaluator="ast_grep",
            evidence="no rules provided -> trivially pass",
            is_ground_truth=True,
            raw={"rules": []},
        )

    score = 1.0
    details: list[str] = []
    violations = 0
    for r in rules:
        ok, ev = _check_one(r, source)
        details.append(("OK " if ok else "X  ") + ev)
        if not ok:
            score -= 0.5
            violations += 1
    score = max(0.0, min(1.0, score))
    return AxisScore(
        name=name,
        score=score,
        weight=weight,
        evaluator="ast_grep",
        evidence=f"{len(rules) - violations}/{len(rules)} rules pass",
        is_ground_truth=True,
        raw={"rules": rules, "details": details, "violations": violations},
    )


__all__ = ["run_ast_grep"]
