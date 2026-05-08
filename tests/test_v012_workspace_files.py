"""v0.12.0 — generalized workspace file extraction tests."""
from __future__ import annotations

from agent_loop.workers import (
    _extract_workspace_files,
    _extract_test_subtask_files,
    _is_safe_workspace_filename,
)


# ---------------------------------------------------------------------------
# filename safety
# ---------------------------------------------------------------------------
def test_safe_basic_filenames() -> None:
    assert _is_safe_workspace_filename("solution.py")
    assert _is_safe_workspace_filename("manuscript.md")
    assert _is_safe_workspace_filename("test_subtask1.py")
    assert _is_safe_workspace_filename("a-b_c.json")


def test_unsafe_path_traversal_rejected() -> None:
    assert not _is_safe_workspace_filename("../etc/passwd")
    assert not _is_safe_workspace_filename("../../foo.txt")
    assert not _is_safe_workspace_filename("..foo")


def test_unsafe_absolute_or_dirpath_rejected() -> None:
    assert not _is_safe_workspace_filename("/etc/passwd")
    assert not _is_safe_workspace_filename("dir/file.py")
    assert not _is_safe_workspace_filename("dir\\file.py")


def test_dotfiles_rejected() -> None:
    assert not _is_safe_workspace_filename(".bashrc")
    assert not _is_safe_workspace_filename(".env")
    assert not _is_safe_workspace_filename(".")
    assert not _is_safe_workspace_filename("..")


def test_special_chars_rejected() -> None:
    assert not _is_safe_workspace_filename("file with spaces.py")
    assert not _is_safe_workspace_filename("file*.py")
    assert not _is_safe_workspace_filename("")


# ---------------------------------------------------------------------------
# generic extractor
# ---------------------------------------------------------------------------
PYTHON_ONLY = """\
# notes

```python
# file: solution.py
def f(): pass
```

trailing prose
"""

MULTI_LANG = """\
## constraints

```python
# file: solution.py
def parse(s): return s.split()
```

```markdown
# file: manuscript.md
# Title

## Abstract
foo
```

```json
# file: config.json
{"version": 1}
```
"""

UNSAFE_HEADER = """\
```python
# file: ../../etc/passwd
print('pwn')
```
"""

NO_HEADER = """\
```python
def f(): pass
```

```python
def g(): pass
```
"""


def test_generic_extract_python_with_header() -> None:
    files = _extract_workspace_files(PYTHON_ONLY)
    assert "solution.py" in files
    assert "def f(): pass" in files["solution.py"]


def test_generic_extract_multiple_languages() -> None:
    files = _extract_workspace_files(MULTI_LANG)
    assert set(files.keys()) == {"solution.py", "manuscript.md", "config.json"}
    assert "def parse" in files["solution.py"]
    assert "## Abstract" in files["manuscript.md"]
    assert '"version": 1' in files["config.json"]


def test_generic_extract_blocks_traversal() -> None:
    files = _extract_workspace_files(UNSAFE_HEADER)
    assert files == {}  # rejected, never saved


def test_generic_extract_skips_headerless_blocks() -> None:
    files = _extract_workspace_files(NO_HEADER)
    assert files == {}  # extractor itself is strict; legacy fallback is in run_implement


# ---------------------------------------------------------------------------
# backward-compat: test_subtask wrapper
# ---------------------------------------------------------------------------
def test_test_subtask_wrapper_filters_correctly() -> None:
    src = """
```python
# file: solution.py
def f(): pass
```

```python
# file: test_subtask1.py
def test_x(): pass
```

```python
# file: helper.py
def h(): pass
```
"""
    test_files = _extract_test_subtask_files(src)
    assert set(test_files.keys()) == {"test_subtask1.py"}


def test_test_subtask_wrapper_empty_when_none() -> None:
    src = """
```python
# file: solution.py
pass
```
"""
    assert _extract_test_subtask_files(src) == {}


# ---------------------------------------------------------------------------
# multi-language comment styles
# ---------------------------------------------------------------------------
def test_html_comment_header() -> None:
    src = """
```html
<!-- file: page.html -->
<h1>hi</h1>
```
"""
    files = _extract_workspace_files(src)
    assert "page.html" in files
    assert "<h1>hi</h1>" in files["page.html"]


def test_js_comment_header() -> None:
    src = """
```javascript
// file: app.js
console.log("hi");
```
"""
    files = _extract_workspace_files(src)
    assert "app.js" in files
    assert "console.log" in files["app.js"]


def test_workspace_prefix_stripped() -> None:
    """LLMs often emit `# file: workspace/foo.md` when plan referenced the
    full path. The extractor strips this leniently."""
    src = """
```markdown
# file: workspace/manuscript.md
# Title
content
```
"""
    files = _extract_workspace_files(src)
    assert "manuscript.md" in files
    assert "workspace/manuscript.md" not in files
    assert "# Title" in files["manuscript.md"]


def test_dotslash_prefix_stripped() -> None:
    src = """
```python
# file: ./solution.py
def f(): pass
```
"""
    files = _extract_workspace_files(src)
    assert "solution.py" in files


def test_traversal_after_strip_still_blocked() -> None:
    """Stripping `workspace/` does not whitelist further traversal."""
    src = """
```python
# file: workspace/../etc/passwd
malicious
```
"""
    files = _extract_workspace_files(src)
    assert files == {}


def test_blank_lines_before_header_accepted() -> None:
    """Codex review fix: a fenced block whose first non-blank line is the
    `# file:` header must be accepted, even if blank lines come first."""
    src = """
```python


# file: solution.py
def f(): pass
```
"""
    files = _extract_workspace_files(src)
    assert "solution.py" in files
    assert "def f(): pass" in files["solution.py"]


def test_duplicate_filename_last_wins() -> None:
    src = """
```python
# file: solution.py
old_version = 1
```

```python
# file: solution.py
new_version = 2
```
"""
    files = _extract_workspace_files(src)
    assert "new_version = 2" in files["solution.py"]
    assert "old_version" not in files["solution.py"]
