"""每库扫描配置：深度与排除规则过滤测试。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from subangler.core.subliminal_adapter import collect_video_paths, _match_excluded


def _touch(root: Path, rel: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    return p


def _rel_paths(root: Path, paths: list[str]) -> set[str]:
    """统一用正斜杠比较，兼容 Windows。"""
    return {str(Path(p).relative_to(root)).replace("\\", "/") for p in paths}


@pytest.fixture
def media_tree(tmp_path: Path) -> Path:
    root = tmp_path / "library"
    root.mkdir()
    for rel in (
        "root.mkv",
        "Season 1/ep01.mkv",
        "Season 1/ep02.mp4",
        "Season 1/extras/sample.mkv",
        "Season 2/sub/ep03.mkv",
        "@eaDir/thumb.mkv",
        ".hidden/secret.mkv",
        "Season 1/poster.jpg",
        "Season 1/info.nfo",
    ):
        _touch(root, rel)
    return root


def test_depth_limits_recursion(media_tree: Path):
    assert _rel_paths(media_tree, collect_video_paths(media_tree, scan_depth=1)) == {"root.mkv"}
    assert _rel_paths(media_tree, collect_video_paths(media_tree, scan_depth=2)) == {
        "root.mkv", "Season 1/ep01.mkv", "Season 1/ep02.mp4", "@eaDir/thumb.mkv",
    }


def test_exclude_patterns(media_tree: Path):
    paths = collect_video_paths(media_tree, exclude_patterns=["*sample*", "*.nfo"])
    rel = _rel_paths(media_tree, paths)
    assert "Season 1/extras/sample.mkv" not in rel
    assert "Season 1/ep01.mkv" in rel
    assert "root.mkv" in rel


def test_exclude_directory_prunes_subtree(media_tree: Path):
    rel = _rel_paths(media_tree, collect_video_paths(
        media_tree, exclude_patterns=["@eaDir", "Season 2"]
    ))
    assert not any(p.startswith("@eaDir") for p in rel)
    assert not any(p.startswith("Season 2") for p in rel)
    assert "Season 1/ep01.mkv" in rel


def test_hidden_and_sample_always_skipped(media_tree: Path):
    paths = collect_video_paths(media_tree)
    rel = _rel_paths(media_tree, paths)
    assert ".hidden/secret.mkv" not in rel
    assert not any(part.lower() == "sample" for p in rel for part in p.split("/"))
    assert "Season 1/poster.jpg" not in rel
    assert os.path.isabs(paths[0])


def test_match_excluded_glob_on_relpath_and_name():
    assert _match_excluded("Season 1/ep01.mkv", "ep01.mkv", ["*.mkv"])
    assert _match_excluded("Season 1/ep01.mkv", "ep01.mkv", ["Season 1/*"])
    assert _match_excluded("a/b.mkv", "b.mkv", ["a\\b.mkv"])
    assert not _match_excluded("Season 1/ep01.mkv", "ep01.mkv", ["Season 2/*"])
