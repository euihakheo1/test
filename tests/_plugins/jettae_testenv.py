"""pytest plugin (``-p jettae_testenv`` in pyproject.toml): a hermetic environment per run.

Why: the CLI root callback and ``require_valid_environment`` load ``JETTAE_ENV_FILE`` or
``./.env`` into ``os.environ``. README tells live-LLM users to
``export JETTAE_ENV_FILE=.env.live-llm``; a test run in that shell (or with an edited ``.env``
in the working directory) would read real provider keys, a live mode and a budget, and the
results would depend on that file. Paid-call isolation must not rest on the tenant-consent and
budget gates alone, so every ``JETTAE_*`` and provider variable is removed before collection
and the env-file lookup is pointed at an empty file.

Kept: ``JETTAE_TEST_PG_URL``, the explicit opt-in to a throwaway PostgreSQL database
(``deploy/run_pg_tests.py``, CI ``postgres`` job). Tests that exercise ``.env`` loading set
their own variables and restore the environment afterwards.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from typing import Any

_KEEP = frozenset({"JETTAE_TEST_PG_URL"})
_PREFIXES = ("JETTAE_", "ANTHROPIC_", "OPENAI_")
_TMP: list[str] = []


def pytest_configure(config: Any) -> None:
    for key in list(os.environ):
        if key.upper().startswith(_PREFIXES) and key.upper() not in _KEEP:
            del os.environ[key]
    tmp = tempfile.mkdtemp(prefix="jettae-tests-")
    _TMP.append(tmp)
    empty = os.path.join(tmp, "empty.env")
    with open(empty, "w", encoding="utf-8"):
        pass
    # an explicit, empty env file: neither the shell's file nor ./.env is read
    os.environ["JETTAE_ENV_FILE"] = empty
    os.environ["JETTAE_LLM_MODE"] = "offline"
    os.environ["JETTAE_LLM_BUDGET_KRW"] = "0"


def pytest_unconfigure(config: Any) -> None:
    while _TMP:
        shutil.rmtree(_TMP.pop(), ignore_errors=True)
