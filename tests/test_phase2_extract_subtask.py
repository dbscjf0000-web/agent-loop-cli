"""Step B — _extract_test_subtask_files unit tests."""
from __future__ import annotations

from agent_loop.workers import _extract_python, _extract_test_subtask_files


SOLUTION_ONLY = """\
## notes
- foo

```python
def is_palindrome(s):
    return s == s[::-1]
```

## review
"""

SOLUTION_PLUS_TWO_TESTS = """\
```python
def parse(s):
    return [int(x) for x in s.split()]
def algo(xs):
    return sorted(xs)
```

```python
# file: test_subtask1.py
from solution import parse
def test_parse_basic():
    assert parse("1 2 3") == [1, 2, 3]
```

```python
# file: test_subtask2.py
from solution import algo
def test_algo_sorted():
    assert algo([3, 1, 2]) == [1, 2, 3]
```
"""

SOLUTION_PLUS_UNMARKED = """\
```python
def f():
    pass
```

```python
def helper():
    return 1
```
"""


def test_solution_only_no_subtask_tests() -> None:
    code, _ = _extract_python(SOLUTION_ONLY)
    assert "is_palindrome" in code
    assert _extract_test_subtask_files(SOLUTION_ONLY) == {}


def test_two_subtask_tests_extracted() -> None:
    files = _extract_test_subtask_files(SOLUTION_PLUS_TWO_TESTS)
    assert set(files.keys()) == {"test_subtask1.py", "test_subtask2.py"}
    assert "test_parse_basic" in files["test_subtask1.py"]
    assert "test_algo_sorted" in files["test_subtask2.py"]
    # solution.py extraction unchanged
    code, _ = _extract_python(SOLUTION_PLUS_TWO_TESTS)
    assert "def parse" in code
    assert "def test_" not in code  # tests not leaked into solution


def test_unmarked_blocks_ignored() -> None:
    """Backward-compat: blocks without `# file: test_subtask*.py` header
    must be silently ignored, not saved as test files."""
    files = _extract_test_subtask_files(SOLUTION_PLUS_UNMARKED)
    assert files == {}


def test_header_must_match_pattern() -> None:
    """`# file: foo.py` (not test_subtask*) → ignored."""
    src = """```python
def f():
    pass
```

```python
# file: test_other.py
def test_x(): pass
```
"""
    assert _extract_test_subtask_files(src) == {}


def test_header_with_extra_spaces_accepted() -> None:
    src = """```python
def f(): pass
```

```python
#  file:  test_subtask99.py
def test_z(): pass
```
"""
    files = _extract_test_subtask_files(src)
    assert "test_subtask99.py" in files
