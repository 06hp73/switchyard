"""notify(title, message, cfg): opt-in macOS desktop notifications.

cfg.notify == "macos" shells out to osascript; anything else (default
"none") is a silent no-op. Every subprocess call is best-effort - it must
never raise, regardless of what the subprocess call does.
"""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "lib"))

from notify import notify  # noqa: E402


def test_none_mode_never_calls_subprocess(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append((a, k)))

    notify("Switchyard", "claude/good landed", SimpleNamespace(notify="none"))

    assert calls == []


def test_unrecognized_mode_never_calls_subprocess(monkeypatch):
    # A typo/unknown value in switchyard.toml must degrade to silence, the
    # same way every other SwitchyardConfig-driven tool in this repo does.
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append((a, k)))

    notify("Switchyard", "claude/good landed", SimpleNamespace(notify="slack"))

    assert calls == []


def test_macos_mode_calls_osascript_with_title_and_message(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append((a, k)))

    notify("Switchyard", "claude/good landed", SimpleNamespace(notify="macos"))

    assert len(calls) == 1
    argv = calls[0][0][0]
    assert argv[0] == "osascript"
    assert argv[1] == "-e"
    script = argv[2]
    assert "display notification" in script
    assert '"claude/good landed"' in script
    assert 'with title "Switchyard"' in script


def test_quotes_and_newlines_in_message_do_not_break_the_script(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append((a, k)))

    notify("t", 'line one\nline two "quoted"', SimpleNamespace(notify="macos"))

    script = calls[0][0][0][2]
    # A literal newline must never survive into the AppleScript string
    # literal, and an embedded double quote must be escaped, not left bare.
    assert "\n" not in script
    assert 'quoted\\"' in script


def test_osascript_missing_never_raises(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("osascript not found")

    monkeypatch.setattr(subprocess, "run", boom)

    notify("t", "m", SimpleNamespace(notify="macos"))  # must not raise


def test_osascript_timeout_never_raises(monkeypatch):
    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="osascript", timeout=10)

    monkeypatch.setattr(subprocess, "run", boom)

    notify("t", "m", SimpleNamespace(notify="macos"))  # must not raise
