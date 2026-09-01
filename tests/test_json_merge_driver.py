"""Key-path 3-way merge for the translations catalog.

Different keys edited on each side -> both survive, valid JSON, exit 0.
Same key edited on both sides -> exit 1 and conflict markers (never corrupt JSON
silently, unlike git's merge=union which interleaves lines in random order).
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

DRIVER = Path(__file__).resolve().parents[1] / "tools" / "train" / "json_merge_driver.py"


def body(merged: str) -> Any:
    """The JSON object encoded in `merged`, ignoring any conflict-marker block."""
    return json.loads(merged.split("<<<<<<<")[0])


def merge(tmp_path: Path, base: dict, ours: dict, theirs: dict) -> tuple[int, str]:
    paths = []
    for name, data in [("base", base), ("ours", ours), ("theirs", theirs)]:
        p = tmp_path / f"{name}.json"
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True))
        paths.append(p)
    proc = subprocess.run(
        [sys.executable, str(DRIVER), *map(str, paths)],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, paths[1].read_text()


def merge_full(tmp_path: Path, base: dict, ours: dict, theirs: dict) -> tuple[int, str, str]:
    """Like merge(), but also returns stderr so a crash can't hide as exit-1."""
    paths = []
    for name, data in [("base", base), ("ours", ours), ("theirs", theirs)]:
        p = tmp_path / f"{name}.json"
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True))
        paths.append(p)
    proc = subprocess.run(
        [sys.executable, str(DRIVER), *map(str, paths)],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, paths[1].read_text(), proc.stderr


def test_disjoint_key_edits_merge_cleanly(tmp_path):
    base = {"pages": {"a": {"title": "A"}, "b": {"title": "B"}}}
    ours = {"pages": {"a": {"title": "A NEW"}, "b": {"title": "B"}}}
    theirs = {"pages": {"a": {"title": "A"}, "b": {"title": "B NEW"}}}
    code, merged = merge(tmp_path, base, ours, theirs)
    assert code == 0
    data = json.loads(merged)
    assert data["pages"]["a"]["title"] == "A NEW"
    assert data["pages"]["b"]["title"] == "B NEW"


def test_added_keys_from_both_sides_survive(tmp_path):
    base = {"x": {"k1": "v1"}}
    ours = {"x": {"k1": "v1", "k2": "ours"}}
    theirs = {"x": {"k1": "v1", "k3": "theirs"}}
    code, merged = merge(tmp_path, base, ours, theirs)
    assert code == 0
    assert json.loads(merged)["x"] == {"k1": "v1", "k2": "ours", "k3": "theirs"}


def test_same_key_conflict_is_loud(tmp_path):
    base = {"x": {"k": "orig"}}
    ours = {"x": {"k": "ours"}}
    theirs = {"x": {"k": "theirs"}}
    code, merged = merge(tmp_path, base, ours, theirs)
    assert code == 1
    assert "<<<<<<<" in merged and ">>>>>>>" in merged


def test_deletion_vs_untouched_applies_deletion(tmp_path):
    base = {"x": {"k1": "v1", "k2": "v2"}}
    ours = {"x": {"k1": "v1"}}
    theirs = {"x": {"k1": "v1", "k2": "v2"}}
    code, merged = merge(tmp_path, base, ours, theirs)
    assert code == 0
    assert json.loads(merged)["x"] == {"k1": "v1"}


def test_output_is_sorted_and_stable(tmp_path):
    base = {"b": 1, "a": 2}
    code, merged = merge(tmp_path, base, base, base)
    assert code == 0
    assert merged.index('"a"') < merged.index('"b"')


def test_empty_dict_values_survive(tmp_path):
    base = {"sidecars": {"placeholders": {}, "tabLabels": {}}, "t": {"k": "v"}}
    ours = {"sidecars": {"placeholders": {}, "tabLabels": {}}, "t": {"k": "v2"}}
    theirs = {"sidecars": {"placeholders": {}, "tabLabels": {}}, "t": {"k": "v"}}
    code, merged = merge(tmp_path, base, ours, theirs)
    assert code == 0
    data = json.loads(merged)
    assert data["sidecars"]["placeholders"] == {}
    assert data["sidecars"]["tabLabels"] == {}
    assert data["t"]["k"] == "v2"


def test_empty_dict_added_on_one_side_survives(tmp_path):
    base = {"a": {"k": "v"}}
    ours = {"a": {"k": "v"}, "new": {}}
    theirs = {"a": {"k": "v"}}
    code, merged = merge(tmp_path, base, ours, theirs)
    assert code == 0
    assert json.loads(merged)["new"] == {}


def test_structural_collapse_conflicts_loudly_not_crash(tmp_path):
    base = {"x": {"k": "v"}}
    ours = {"x": "flat"}
    theirs = {"x": {"k": "v2"}}
    code, merged, stderr = merge_full(tmp_path, base, ours, theirs)
    assert code == 1
    assert "<<<<<<<" in merged and ">>>>>>>" in merged
    assert "Traceback" not in stderr


def test_structural_collapse_with_added_subkey_does_not_crash(tmp_path):
    # Base has no "k2". Ours collapses "x" to a scalar; theirs keeps "x"
    # nested AND adds a brand-new sub-key under it, so that sub-key has no
    # base value to fall back on. That is the exact shape that made the
    # pre-fix merge write real leaves at both "x" and "x.k2" at once and
    # crash inside unflatten() with "TypeError: 'str' object does not
    # support item assignment" -- verified against the pre-fix driver.
    base = {"x": {"k1": "v1"}}
    ours = {"x": "flat"}
    theirs = {"x": {"k1": "v1", "k2": "NEW"}}
    code, merged, stderr = merge_full(tmp_path, base, ours, theirs)
    assert code == 1
    assert "<<<<<<<" in merged and ">>>>>>>" in merged
    assert "Traceback" not in stderr


def test_real_catalog_identity_merge_lossless(tmp_path):
    # Bundled fixture carries the shapes that broke earlier driver versions
    # (empty-dict values, non-ASCII text). Point SWITCHYARD_CATALOG at a real
    # project catalog to run the same losslessness check against it.
    import os

    default = Path(__file__).resolve().parent / "data" / "catalog_fixture.json"
    catalog = Path(os.environ.get("SWITCHYARD_CATALOG", default))
    data = json.loads(catalog.read_text())
    code, merged = merge(tmp_path, data, data, data)
    assert code == 0
    assert json.loads(merged) == data


def test_collapse_by_theirs_keeps_ours_subtree_in_body(tmp_path):
    base = {"x": {"k1": "v1"}}
    ours = {"x": {"k1": "v1", "k2": "NEW"}}
    theirs = {"x": "flat"}
    code, merged = merge(tmp_path, base, ours, theirs)
    assert code == 1
    assert "<<<<<<<" in merged
    data = json.loads(merged.split("<<<<<<<")[0])
    assert data["x"] == {"k1": "v1", "k2": "NEW"}


def test_delete_vs_edit_marker_is_readable(tmp_path):
    base = {"x": {"k": "orig"}}
    ours = {"x": {}}
    theirs = {"x": {"k": "edited"}}
    code, merged = merge(tmp_path, base, ours, theirs)
    assert code == 1
    assert "object object" not in merged
    assert "<deleted>" in merged


def test_all_sides_empty_merges_to_empty_object(tmp_path):
    # Trivial identity: every side collapses the whole document to {}. flatten
    # plants a leaf at the root path () for each, and they all agree, so this
    # must merge cleanly -- not trip the structural-collision machinery just
    # because a leaf happens to sit at the root.
    code, merged, stderr = merge_full(tmp_path, {}, {}, {})
    assert code == 0
    assert body(merged) == {}
    assert "Traceback" not in stderr


def test_whole_document_collapse_by_ours_conflicts_at_root(tmp_path):
    # Ours collapses the ENTIRE document to {} while theirs still has real
    # content underneath "x". flatten()'s empty-dict-as-leaf rule plants a
    # leaf at the root path () -- prefix_collisions() must flag that leaf as
    # colliding with every other (longer) path, since () is a prefix of all
    # of them, or unflatten() crashes on path[-1] with an empty path tuple.
    # Verified against the pre-fix driver: IndexError: tuple index out of range.
    base = {"x": {"k": "v"}}
    ours = {}
    theirs = {"x": "flat"}
    code, merged, stderr = merge_full(tmp_path, base, ours, theirs)
    assert code == 1
    assert "Traceback" not in stderr
    assert "<<<<<<<" in merged and ">>>>>>>" in merged
    assert "(root)" in merged
    assert body(merged) == {}


def test_whole_document_collapse_by_theirs_keeps_ours_document(tmp_path):
    # Mirror of the above: theirs collapses to {}, ours keeps real content.
    # The root-level structural conflict must still resolve toward ours'
    # whole document in the body, exactly like a non-root structural collapse
    # already does (see test_collapse_by_theirs_keeps_ours_subtree_in_body).
    base = {"x": {"k": "v"}}
    ours = {"x": {"k": "v2"}}
    theirs = {}
    code, merged = merge(tmp_path, base, ours, theirs)
    assert code == 1
    assert "<<<<<<<" in merged and ">>>>>>>" in merged
    assert body(merged) == ours
