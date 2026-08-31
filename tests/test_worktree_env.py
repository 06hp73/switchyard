"""The allocator hands each worktree a stable, distinct port/Redis slot."""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "guards" / "worktree_env.sh"


def run_alloc(worktree_name: str) -> dict[str, str]:
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env={"PATH": "/usr/bin:/bin", "EV4XL_WORKTREE_NAME": worktree_name},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    out: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if line.startswith("export "):
            key, _, value = line.removeprefix("export ").partition("=")
            out[key] = value.strip('"')
    return out


def test_exports_all_slots():
    env = run_alloc("parallel-fix-collisions-b2d1f3")
    assert set(env) == {"EV4XL_PORT_OFFSET", "EV4XL_DASH_PORT", "EV4XL_API_PORT", "EV4XL_REDIS_DB"}


def test_stable_for_same_name():
    assert run_alloc("alpha") == run_alloc("alpha")


def test_distinct_for_different_names():
    a, b = run_alloc("alpha"), run_alloc("beta")
    assert a["EV4XL_DASH_PORT"] != b["EV4XL_DASH_PORT"]
    assert a["EV4XL_REDIS_DB"] != b["EV4XL_REDIS_DB"]


def test_ports_in_safe_range_and_off_8050():
    for name in ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta"]:
        env = run_alloc(name)
        dash = int(env["EV4XL_DASH_PORT"])
        assert 8100 <= dash <= 8899
        assert dash != 8050
        assert 0 <= int(env["EV4XL_REDIS_DB"]) <= 15
