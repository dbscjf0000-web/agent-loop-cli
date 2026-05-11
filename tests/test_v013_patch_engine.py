"""v0.13 — patch engine unit tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_loop.patch_engine import Patch, apply_patches, parse_patches


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
SINGLE = """\
prose before

```search-replace
file: solution.py
<<<<<<< SEARCH
def slow(): return 1
=======
def fast(): return 2
>>>>>>> REPLACE
```

trailing prose
"""

TWO_BLOCKS = """\
```search-replace
file: a.py
<<<<<<< SEARCH
old_a
=======
new_a
>>>>>>> REPLACE
```

```search-replace
file: b.md
<<<<<<< SEARCH
old text
=======
new text
>>>>>>> REPLACE
```
"""

MALFORMED = """\
```search-replace
file: x.py
<<<<<<< SEARCH
no divider here
>>>>>>> REPLACE
```

```search-replace
no_file_header
<<<<<<< SEARCH
foo
=======
bar
>>>>>>> REPLACE
```
"""


def test_parse_single_block() -> None:
    out = parse_patches(SINGLE)
    assert len(out) == 1
    assert out[0].file == "solution.py"
    assert "slow()" in out[0].search
    assert "fast()" in out[0].replace


def test_parse_multiple_blocks() -> None:
    out = parse_patches(TWO_BLOCKS)
    assert [p.file for p in out] == ["a.py", "b.md"]


def test_parse_skips_malformed() -> None:
    """Blocks missing the divider or file header are dropped, not crash."""
    out = parse_patches(MALFORMED)
    assert out == []


def test_parse_empty_text() -> None:
    assert parse_patches("") == []


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------
def test_apply_exact_match(tmp_path: Path) -> None:
    (tmp_path / "solution.py").write_text("def slow(): return 1\n", encoding="utf-8")
    patches = [Patch(file="solution.py", search="def slow(): return 1", replace="def fast(): return 2")]
    res = apply_patches(tmp_path, patches)
    assert res.ok and len(res.applied) == 1
    assert (tmp_path / "solution.py").read_text(encoding="utf-8") == "def fast(): return 2\n"


def test_apply_missing_file(tmp_path: Path) -> None:
    patches = [Patch(file="nope.py", search="x", replace="y")]
    res = apply_patches(tmp_path, patches)
    assert not res.ok
    assert "does not exist" in res.failed[0][1]


def test_apply_creates_file_when_search_empty(tmp_path: Path) -> None:
    patches = [Patch(file="new.md", search="", replace="# Hello")]
    res = apply_patches(tmp_path, patches)
    assert res.ok
    assert (tmp_path / "new.md").read_text(encoding="utf-8").rstrip() == "# Hello"


def test_apply_appends_when_search_empty(tmp_path: Path) -> None:
    (tmp_path / "log.md").write_text("first line\n", encoding="utf-8")
    patches = [Patch(file="log.md", search="", replace="second line")]
    res = apply_patches(tmp_path, patches)
    assert res.ok
    body = (tmp_path / "log.md").read_text(encoding="utf-8")
    assert "first line" in body and "second line" in body


def test_apply_ambiguous_match_fails(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("foo\nfoo\n", encoding="utf-8")
    patches = [Patch(file="x.py", search="foo", replace="bar")]
    res = apply_patches(tmp_path, patches)
    assert not res.ok
    assert "matches 2 times" in res.failed[0][1]


def test_apply_no_match_fails(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("hello\n", encoding="utf-8")
    patches = [Patch(file="x.py", search="bye", replace="ciao")]
    res = apply_patches(tmp_path, patches)
    assert not res.ok
    assert "not found" in res.failed[0][1]


def test_apply_rejects_unsafe_filename(tmp_path: Path) -> None:
    patches = [Patch(file="../etc/passwd", search="x", replace="y")]
    res = apply_patches(tmp_path, patches)
    assert not res.ok
    assert "unsafe" in res.failed[0][1]


def test_apply_sequential_order(tmp_path: Path) -> None:
    """Later patches see the result of earlier ones in the same batch."""
    (tmp_path / "x.txt").write_text("A B C\n", encoding="utf-8")
    patches = [
        Patch(file="x.txt", search="A", replace="A1"),
        Patch(file="x.txt", search="A1", replace="A2"),
    ]
    res = apply_patches(tmp_path, patches)
    assert res.ok and len(res.applied) == 2
    assert "A2 B C" in (tmp_path / "x.txt").read_text(encoding="utf-8")
