"""Best-effort desktop notifications for long-running switchyard commands.

notify(title, message, cfg) is the single entry point: `cfg` is anything
with a `.notify` attribute (a real SwitchyardConfig, or a small throwaway
stand-in for callers like merge_train.py's run_train() that deliberately
take plain parameters rather than a whole config object - see that module's
docstring). "macos" shells out to `osascript` for a Notification Center
banner; "none" (the default) - and anything else unrecognized - is a no-op,
so a typo in switchyard.toml degrades to silence rather than an error.

A notification is purely informational: it must never affect the thing it
is reporting on. Every subprocess call here is best-effort - a missing
`osascript`, a hung one, or any other failure is swallowed, never raised.
"""

from __future__ import annotations

import subprocess


def notify(title: str, message: str, cfg) -> None:
    """Fire a desktop notification per cfg.notify. Never raises."""
    if cfg.notify == "macos":
        _notify_macos(title, message)
    # else: "none" or anything unrecognized - silent no-op by design.


def _notify_macos(title: str, message: str) -> None:
    script = (
        f"display notification {_applescript_string(message)} "
        f"with title {_applescript_string(title)}"
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass  # best-effort: a missing/hung osascript must never break the caller


def _applescript_string(value: str) -> str:
    """Quote `value` as a double-quoted AppleScript string literal.

    AppleScript's only in-string escapes are a doubled backslash and a
    doubled/backslashed quote - there is no backslash-n - so a literal
    newline is flattened to a space (a notification renders as one line
    anyway) rather than risk closing the string early or emitting invalid
    script text.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{escaped}"'
