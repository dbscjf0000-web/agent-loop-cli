"""v0.16 — lenient _extract_json tests (trailing comma, smart quotes,
4-backtick fences, prose-wrapped fences)."""
from __future__ import annotations

import pytest

from agent_loop.workers import _extract_json, _json_lenient_loads


def test_strict_json_still_works() -> None:
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_three_backtick_json_fence() -> None:
    src = '```json\n{"a": 2}\n```'
    assert _extract_json(src) == {"a": 2}


def test_four_backtick_json_fence() -> None:
    """v0.16 catch: LLMs sometimes wrap with ```` to avoid nested-fence
    truncation. Parser must recognise the 4-backtick form."""
    src = '````json\n{"a": 3}\n````'
    assert _extract_json(src) == {"a": 3}


def test_trailing_comma_in_object() -> None:
    """Sonnet very often emits trailing commas — Python json.loads rejects
    them. v0.16 lenient pass strips them before retry."""
    src = '```json\n{"a": 4, "b": 5,}\n```'
    assert _extract_json(src) == {"a": 4, "b": 5}


def test_trailing_comma_in_nested_array() -> None:
    src = '{"xs": [1, 2, 3,], "y": 6,}'
    assert _extract_json(src) == {"xs": [1, 2, 3], "y": 6}


def test_smart_quotes_normalized() -> None:
    src = '{“key”: “value”}'
    assert _extract_json(src) == {"key": "value"}


def test_prose_wrapped_fence_with_trailing_comma() -> None:
    """Real-world sonnet judge output: a paragraph before, the JSON in a
    fenced block, and an extra trailing comma."""
    src = (
        "Here is my judgment:\n\n"
        "```json\n"
        '{"better": false, "action": "redo_P", "scores": {"this_cycle": 0.7424,},}\n'
        "```\n\n"
        "Done."
    )
    out = _extract_json(src)
    assert out["action"] == "redo_P"
    assert out["scores"]["this_cycle"] == pytest.approx(0.7424)


def test_brace_slice_fallback_with_trailing_comma() -> None:
    """Even without a fence, the brace-slice + lenient pass should recover
    a comma-laden JSON object."""
    src = 'noise before {"a": 7,} noise after'
    assert _extract_json(src) == {"a": 7}


def test_lenient_loads_smart_quotes_direct() -> None:
    assert _json_lenient_loads('{“a”: 1}') == {"a": 1}


def test_unparseable_still_raises() -> None:
    """A non-JSON-looking string should still raise so the caller's
    "halt to be safe" fallback engages."""
    import json as _json
    with pytest.raises((ValueError, _json.JSONDecodeError)):
        _extract_json("not json at all, just prose")
