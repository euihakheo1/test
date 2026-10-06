"""Start a throw-away PostgreSQL (pgserver) and run the PostgreSQL-only tests on it
(tests/api/test_api_postgres.py and the PostgreSQL case of
tests/llm_agents/test_la_budget_store.py).

    uv run --with pgserver python deploy/run_pg_tests.py <short-tmp-dir>

pgserver is NOT a project dependency; ``uv run --with`` adds it to a temporary overlay.
"""

from __future__ import annotations

import os
import subprocess
import sys


def main() -> int:
    import pgserver  # type: ignore[import-not-found]

    workdir = sys.argv[1] if len(sys.argv) > 1 else os.path.abspath("var/pgtest")
    os.makedirs(workdir, exist_ok=True)
    srv = pgserver.get_server(os.path.join(workdir, "pgdata"), cleanup_mode="stop")
    srv.psql("DROP DATABASE IF EXISTS jettae_test;")
    srv.psql("CREATE DATABASE jettae_test;")
    url = srv.get_uri("jettae_test").replace("postgresql://", "postgresql+psycopg://", 1)
    print(srv.psql("select version();").splitlines()[2].strip())
    env = dict(os.environ, JETTAE_TEST_PG_URL=url)
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-rs",
        "tests/api/test_api_postgres.py",
        # shared LLM budget (objects and OS processes) on the same server
        "tests/llm_agents/test_la_budget_store.py",
        "-k",
        "pg_ or postgres",
    ]
    return subprocess.run(cmd, env=env, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
