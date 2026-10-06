"""Importing the package must not touch stdio; the CLI entry configures it (Windows)."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

_PROBE = (
    "import sys; before = sys.stdout.encoding; import jettae, jettae.cli; "
    "after = sys.stdout.encoding; jettae.cli.force_utf8_stdio(); "
    "print(before, after, sys.stdout.encoding)"
)


def _probe() -> list[str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
    env["PYTHONUTF8"] = "0"
    out = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, env=env, check=True
    )
    return out.stdout.split()


def test_import_has_no_stdio_side_effect() -> None:
    before, after, _ = _probe()
    assert before == after


@pytest.mark.skipif(sys.platform != "win32", reason="reconfigure is Windows-only")
def test_cli_entry_switches_to_utf8_on_windows() -> None:
    assert _probe()[2].lower().replace("-", "") == "utf8"
