"""3-way JSON merge by key path, for the translations catalog.

git merge-driver contract: argv = base, ours, theirs (file paths). We write the
merged result to the OURS path. Exit 0 = merged; exit 1 = genuine conflict
(same key path changed differently on both sides, or one side collapsing a
nested object to a scalar while the other still has sub-keys under it) with
conflict markers left in the ours file so the operator sees exactly which key
path collided.

Deliberately strict: any same-key divergence is a real conflict. Never guess.
"""

from __future__ import annotations

import json
import sys
from typing import Any

SENTINEL_DELETED = object()


def flatten(obj: Any, prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    """Flatten nested dicts to {dotted-path-tuple: leaf-value}.

    An empty dict is itself a leaf: flatten({}, prefix) == {prefix: {}}.
    Recursing into it would produce zero entries, so it would vanish through
    flatten -> unflatten with no trace it ever existed. `{}` is a real catalog
    value (an intentionally-empty placeholder group), not "nothing here."
    """
    if isinstance(obj, dict) and obj:
        out: dict[tuple[str, ...], Any] = {}
        for key, value in obj.items():
            out.update(flatten(value, prefix + (str(key),)))
        return out
    return {prefix: obj}


def unflatten(flat: dict[tuple[str, ...], Any]) -> Any:
    # A leaf at the root path () means the *whole document* collapsed to a
    # single value on the winning side (flatten()'s empty-dict-as-leaf rule:
    # flatten({}) == {(): {}}). prefix_collisions() treats () as a prefix of
    # every other path, so a root leaf is always flagged as colliding with
    # anything else and three_way() always resolves that down to one side
    # before calling unflatten() -- a root entry is therefore guaranteed to
    # be the *only* entry whenever it appears here. Without this case,
    # `path[:-1]` on the empty tuple is still `()` (slicing never raises) but
    # `path[-1]` does: IndexError, since there is no last element.
    if () in flat:
        return flat[()]
    root: dict[str, Any] = {}
    for path, value in flat.items():
        node = root
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = value
    return root


def get_path(obj: Any, path: tuple[str, ...]) -> Any:
    """Walk a dotted path through a nested dict; SENTINEL_DELETED if absent."""
    node = obj
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return SENTINEL_DELETED
        node = node[part]
    return node


def prefix_collisions(paths: set[tuple[str, ...]]) -> set[tuple[str, ...]]:
    """Paths that are a strict prefix of some other path in the same set.

    unflatten() turns each flat path into a chain of dict lookups: a node is
    either a leaf value or a dict of children, never both. If the merged flat
    map ends up with a path *and* a longer path under it -- one side collapsed
    an object to a scalar while the other kept or edited sub-keys there --
    unflatten() cannot build both shapes on the same node; it silently
    overwrites one or crashes trying to assign a key into the scalar,
    depending on which path happens to be written first. That is a structural
    conflict, not an ordinary value conflict, and must be caught before
    unflatten() ever sees it.

    The empty tuple () -- the document root -- is itself a valid path here:
    flatten()'s empty-dict-as-leaf rule plants a leaf at () when a whole side
    collapses the entire document to {}. () is a prefix of every other path,
    so a root leaf coexisting with anything else is this same structural
    collision and must be caught the same way. The scan below starts at i=0
    (not i=1) so it checks the empty prefix too; skipping it would let a
    whole-document collapse slip through uncaught and crash unflatten() on
    path[-1] with an empty path tuple.
    """
    collisions: set[tuple[str, ...]] = set()
    for path in paths:
        for i in range(len(path)):
            if path[:i] in paths:
                collisions.add(path[:i])
    return collisions


def three_way(base: dict, ours: dict, theirs: dict) -> tuple[dict, list[tuple]]:
    fb, fo, ft = flatten(base), flatten(ours), flatten(theirs)
    conflicts: list[tuple] = []
    merged: dict[tuple[str, ...], Any] = {}
    for path in sorted(set(fb) | set(fo) | set(ft)):
        b = fb.get(path, SENTINEL_DELETED)
        o = fo.get(path, SENTINEL_DELETED)
        t = ft.get(path, SENTINEL_DELETED)
        if o == t:
            value = o
        elif o == b:
            value = t  # only theirs changed
        elif t == b:
            value = o  # only ours changed
        else:
            conflicts.append((path, o, t))
            value = o
        if value is not SENTINEL_DELETED:
            merged[path] = value

    collided = prefix_collisions(set(merged))
    if collided:
        # A structural conflict below supersedes any per-leaf conflict already
        # recorded at or under the same path -- otherwise the operator would
        # also see a same-key conflict block quoting a raw internal sentinel
        # for a leaf that no longer exists on the collapsed side.
        conflicts = [c for c in conflicts if not any(c[0][: len(p)] == p for p in collided)]
        for prefix in sorted(collided):
            conflicts.append((prefix, get_path(ours, prefix), get_path(theirs, prefix)))
            if prefix in fo:
                # Ours collapsed this path to a leaf -- that shape wins, so
                # drop every entry that would need this node to still be a
                # dict, and make sure ours' own leaf value is what lands here.
                merged[prefix] = fo[prefix]
                for key in [
                    k for k in merged if len(k) > len(prefix) and k[: len(prefix)] == prefix
                ]:
                    del merged[key]
            else:
                # Ours kept the nested object here (or deleted the whole
                # subtree) -- a leaf at this path exists in the per-leaf
                # merge only because theirs/base collapsed it there. Worse,
                # every per-leaf entry still under this prefix was computed
                # against theirs' flat (collapsed) value rather than its
                # real per-leaf values, so a leaf like ours' unchanged
                # `k1` reads as "theirs deleted it" and silently vanishes.
                # Discard everything the per-leaf pass produced at or under
                # this prefix and rebuild the whole subtree wholesale from
                # ours' own flat entries -- ours' shape wins here entirely,
                # not leaf-by-leaf.
                for key in [k for k in merged if k[: len(prefix)] == prefix]:
                    del merged[key]
                for key, value in fo.items():
                    if key[: len(prefix)] == prefix:
                        merged[key] = value

    return unflatten(merged), conflicts


def main() -> int:
    base_path, ours_path, theirs_path = sys.argv[1:4]
    with open(base_path, encoding="utf-8") as f:
        base = json.load(f)
    with open(ours_path, encoding="utf-8") as f:
        ours = json.load(f)
    with open(theirs_path, encoding="utf-8") as f:
        theirs = json.load(f)

    merged, conflicts = three_way(base, ours, theirs)
    rendered = json.dumps(merged, indent=2, ensure_ascii=False, sort_keys=True) + "\n"

    if conflicts:
        lines = [rendered, "\n"]
        for path, o, t in conflicts:
            dotted = ".".join(path) if path else "(root)"
            o_text = "<deleted>" if o is SENTINEL_DELETED else repr(o)
            t_text = "<deleted>" if t is SENTINEL_DELETED else repr(t)
            lines.append(
                f"<<<<<<< ours: {dotted}\n{o_text}\n=======\n{t_text}\n>>>>>>> theirs: {dotted}\n"
            )
        with open(ours_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        print(f"json_merge_driver: {len(conflicts)} conflict(s)", file=sys.stderr)
        return 1

    with open(ours_path, "w", encoding="utf-8") as f:
        f.write(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
