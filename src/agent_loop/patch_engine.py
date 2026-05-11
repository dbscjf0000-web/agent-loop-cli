"""v0.13 — SEARCH/REPLACE patch engine.

When P partitions Implement into ``### stage N`` groups, each worker
emits SEARCH/REPLACE blocks (Aider-style) instead of whole files. The
coordinator parses them and applies in-stage patches against a fresh
snapshot of the workspace, then moves to the next stage.

Public API
----------
parse_patches(text)            -> list[Patch]
apply_patches(workspace, patches) -> ApplyResult
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# Patch format expected from the Implement worker:
#
#     ```search-replace
#     file: <name>
#     <<<<<<< SEARCH
#     <exact substring of file>
#     =======
#     <replacement substring>
#     >>>>>>> REPLACE
#     ```
#
# Multiple blocks per response allowed. file: header gives workspace-relative
# basename (must pass the same _is_safe_workspace_filename policy used by
# workers._extract_workspace_files).
_BLOCK_RE = re.compile(
    r"```search-replace\s*\n(?P<body>.*?)```",
    re.DOTALL,
)
_HEADER_RE = re.compile(r"^\s*file\s*:\s*([A-Za-z0-9_\-.]+)\s*$", re.MULTILINE)


@dataclass
class Patch:
    """A single SEARCH/REPLACE operation."""
    file: str
    search: str
    replace: str
    # Index in the original response (for debugging / decision log)
    seq: int = 0


@dataclass
class ApplyResult:
    """Outcome of applying a batch of patches against the workspace."""
    applied: list[Patch] = field(default_factory=list)
    failed: list[tuple[Patch, str]] = field(default_factory=list)  # (patch, reason)

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def n_total(self) -> int:
        return len(self.applied) + len(self.failed)


def parse_patches(text: str) -> list[Patch]:
    """Extract every ```search-replace``` block from ``text``.

    Tolerant: malformed blocks (missing file: header, no SEARCH marker, etc.)
    are skipped silently — caller decides via the count whether anything
    was produced. Keeping the parse lenient means an LLM that emits a
    half-formed block doesn't abort the entire stage.
    """
    out: list[Patch] = []
    for seq, m in enumerate(_BLOCK_RE.finditer(text)):
        body = m.group("body")
        header = _HEADER_RE.search(body)
        if not header:
            continue
        fname = header.group(1).strip()
        # Strip the header line from body so the markers are easier to locate.
        body_no_header = body[: header.start()] + body[header.end():]
        # Pull SEARCH / DIVIDER / REPLACE markers.
        try:
            search_start = body_no_header.index("<<<<<<< SEARCH")
            divider = body_no_header.index("=======", search_start)
            replace_end = body_no_header.index(">>>>>>> REPLACE", divider)
        except ValueError:
            continue
        search = body_no_header[search_start + len("<<<<<<< SEARCH") + 1 : divider]
        replace = body_no_header[divider + len("=======") + 1 : replace_end]
        # Strip trailing newline from each side for stable matching.
        out.append(
            Patch(
                file=fname,
                search=search.rstrip("\n"),
                replace=replace.rstrip("\n"),
                seq=seq,
            )
        )
    return out


def apply_patches(workspace: Path, patches: list[Patch]) -> ApplyResult:
    """Apply each patch sequentially against ``workspace/<file>``.

    Patch semantics:
      • exact-substring search → first match replaced
      • search="" → treated as "append at end of file"
      • file does not exist + search="" → create with replacement as content
      • file does not exist + non-empty search → fail
      • multiple matches → fail (caller should split into more specific patches)

    Returns an ApplyResult separating successes from failures with reasons.
    Patches that fail leave the workspace unchanged for their target file.
    """
    from agent_loop.workers import _is_safe_workspace_filename  # shared policy

    result = ApplyResult()
    for p in patches:
        if not _is_safe_workspace_filename(p.file):
            result.failed.append((p, f"unsafe filename: {p.file!r}"))
            continue
        target = workspace / p.file
        if not target.exists():
            if p.search.strip() == "":
                # treat empty search as "create file"
                try:
                    target.write_text(p.replace + "\n", encoding="utf-8")
                    result.applied.append(p)
                except OSError as exc:
                    result.failed.append((p, f"write failed: {exc}"))
            else:
                result.failed.append((p, "file does not exist"))
            continue

        try:
            current = target.read_text(encoding="utf-8")
        except OSError as exc:
            result.failed.append((p, f"read failed: {exc}"))
            continue

        if p.search.strip() == "":
            # append at end with a single newline separator
            new = current.rstrip("\n") + "\n" + p.replace + "\n"
            try:
                target.write_text(new, encoding="utf-8")
                result.applied.append(p)
            except OSError as exc:
                result.failed.append((p, f"write failed: {exc}"))
            continue

        count = current.count(p.search)
        if count == 0:
            result.failed.append((p, "search text not found"))
            continue
        if count > 1:
            result.failed.append(
                (p, f"search text matches {count} times (must be unique)")
            )
            continue
        new = current.replace(p.search, p.replace, 1)
        try:
            target.write_text(new, encoding="utf-8")
            result.applied.append(p)
        except OSError as exc:
            result.failed.append((p, f"write failed: {exc}"))
    return result


__all__ = ["Patch", "ApplyResult", "parse_patches", "apply_patches"]
