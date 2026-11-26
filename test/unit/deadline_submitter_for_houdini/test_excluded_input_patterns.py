# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Tests for how an excluded input path matches an auto-detected input's evaluated path."""
import ntpath
import os
import types
from unittest import mock

import pytest

from deadline.houdini_submitter.python.deadline_cloud_for_houdini import _assets
from deadline.houdini_submitter.python.deadline_cloud_for_houdini._assets import (
    _get_matching_pattern,
)

_POSIX_CASE = pytest.mark.skipif(os.name == "nt", reason="normcase ignores case on Windows")
_WINDOWS_CASE = pytest.mark.skipif(os.name != "nt", reason="normcase keeps case on posix")


def _matches(path: str, pattern: str, whole_path: bool = False) -> bool:
    return _get_matching_pattern(path, [pattern], whole_path) is not None


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        # At or under the pattern, by whole names.
        ("/p/cache", "/p/cache", True),
        ("/p/cache/sh010_sim/v1/a.bgeo.sc", "/p/cache", True),
        ("/p/cache/.tmp/a", "/p/cache", True),
        ("/p", "/p/cache", False),
        ("/p/cache_old", "/p/cache", False),
        ("/p/cache2", "/p/cache", False),
        ("/p/cache/", "/p/cache", True),
        ("/p/cache", "/p/cache/", True),
        ("/p/render", "/p/cache", False),
        # "*" and "?" match within one name.
        ("/p/geo/sim_tmp/f.bgeo", "/p/geo/*_tmp", True),
        ("/p/geo/sim_tmp2", "/p/geo/*_tmp", False),
        ("/p/geo/a/sim_tmp", "/p/geo/*_tmp", False),
        ("/p/render/sh015/v1", "/p/render/sh01?", True),
        ("/p/render/sh020", "/p/render/sh01?", False),
        ("/p/render/sh0100", "/p/render/sh01?", False),
        # "**" matches any number of folders.
        ("/p/shot/cache/v1/a", "**/cache", True),
        ("/p/render/a", "**/cache", False),
        ("/p/a/x", "/p/**/v001", False),
        ("/p/a/b/v001/f", "/p/**/v001", True),
        ("/", "**", True),
        # Wildcards and "**" skip dot names, and a literal dot name matches.
        ("/p/cache/.dot", "/p/cache/*", False),
        ("/p/cache/.dot", "/p/cache/.*", True),
        ("/p/.hid/x/f", "**/f", False),
        ("/p/.hid/x", "**/x", False),
        ("/p/.hid/x", "/p/**/x", False),
        ("/p/.hid/x/f", "/p/.hid", True),
        # A root excludes everything on it.
        ("/p/cache/a", "/", True),
        ("/", "/", True),
        pytest.param("/p/Cache", "/p/cache", False, marks=_POSIX_CASE),
        pytest.param("/p/Cache", "/p/cache", True, marks=_WINDOWS_CASE),
    ],
)
def test_a_path_at_or_under_the_pattern_matches(path: str, pattern: str, expected: bool) -> None:
    assert _matches(path, pattern) is expected


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("/p/cache", False),
        ("/p/cache/**", False),
        ("**", False),
        ("/p/cache/*/a.bgeo", True),
        ("**/*.bgeo", True),
        ("/p/cache/v3/a.bgeo/**", True),
    ],
)
def test_a_whole_path_match_needs_a_pattern_naming_the_path(pattern: str, expected: bool) -> None:
    """A File Cache's read file is left out only by a pattern naming it, not a folder above it."""
    assert _matches("/p/cache/v3/a.bgeo", pattern, whole_path=True) is expected


_PATHS = ["/p", "/p/cache", "/p/cache/v1/a.bgeo.sc", "/p/cache/.tmp/a", "/p/cache_old"]
_PATHS += ["/p/geo/sim_tmp/f", "/p/render/sh015/v1", "/p/shot/cache/v1/a", "/p/.hid/x/f", "/"]


@pytest.mark.parametrize("base", ["/p/cache", "**/cache", "/p/*/v1"])
def test_a_trailing_double_star_changes_nothing(base: str) -> None:
    for path in _PATHS + ["/p/cache2", "/p/cache/v1"]:
        plain: bool = _matches(path, base)
        assert _matches(path, base + "/**") is plain and _matches(path, base + "/**/**") is plain


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        (r"C:\Proj\Cache", "c:/proj/cache", True),
        (r"C:\Proj\Cache\v001\a", "C:/Proj/Cache/*", True),
        (r"C:\Proj\Cache_old", "C:/Proj/Cache", False),
        (r"C:\Proj\Shot\Cache\v001", "**/cache", True),
        (r"C:\Proj\Cache", "C:/", True),
        (r"D:\Proj\Cache", "C:/", False),
        (r"\\srv\share\Cache\a", "//srv/share/cache", True),
        (r"\\srv\other\Cache", "//srv/share", False),
    ],
)
def test_windows_paths(path: str, pattern: str, expected: bool) -> None:
    windows_os = types.SimpleNamespace(path=ntpath, sep="\\", altsep="/")
    with mock.patch.object(_assets, "os", windows_os):
        assert _matches(path, pattern) is expected
