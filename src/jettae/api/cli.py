"""``jettae api serve | migrate | openapi`` (registered by ``jettae.cli``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(help="HTTP API (FastAPI) 실행과 DB 마이그레이션", no_args_is_help=True)


@app.command("serve")
def serve(
    host: Annotated[str | None, typer.Option("--host")] = None,
    port: Annotated[int | None, typer.Option("--port")] = None,
    workers: Annotated[int, typer.Option("--workers", min=1)] = 1,
    log_level: Annotated[str, typer.Option("--log-level")] = "info",
    reload: Annotated[bool, typer.Option("--reload", help="개발용 자동 재시작")] = False,
) -> None:
    """uvicorn으로 API를 실행한다 (설정: JETTAE_* 환경 변수, 없으면 JETTAE_ENV_FILE 또는 ./.env)."""
    import uvicorn

    from jettae.config import require_valid_environment
    from jettae.db.logs import configure_logging

    st = require_valid_environment("api")
    configure_logging(log_level)
    uvicorn.run(
        "jettae.api.main:app_factory",
        factory=True,
        host=host or st.api_host,
        port=port or st.api_port,
        workers=workers,
        log_level=log_level,
        reload=reload,
        proxy_headers=True,
        log_config=None,
    )


@app.command("migrate")
def migrate(
    revision: Annotated[str, typer.Argument(help="대상 리비전")] = "head",
    down: Annotated[bool, typer.Option("--down", help="downgrade")] = False,
) -> None:
    """Alembic 마이그레이션 (JETTAE_DATABASE_URL)."""
    from jettae.config import require_valid_environment
    from jettae.db import migrate as m

    st = require_valid_environment("db")
    if down:
        m.downgrade(st.database_url, revision, st.migrations_dir)
    else:
        m.upgrade(st.database_url, revision, st.migrations_dir)
    typer.echo(f"database {'downgraded' if down else 'upgraded'} to {revision}")


@app.command("openapi")
def openapi(out: Annotated[Path | None, typer.Option("--out")] = None) -> None:
    """OpenAPI 문서를 출력한다 (DB 연결 불필요: 임시 SQLite 사용)."""
    import tempfile

    from jettae.api.main import create_app
    from jettae.db.config import Settings

    with tempfile.TemporaryDirectory() as d:
        st = Settings(database_url=f"sqlite:///{d}/openapi.db", blob_dir=Path(d) / "b", env="test")
        spec = create_app(settings=st).openapi()
    text = json.dumps(spec, ensure_ascii=False, indent=2)
    if out:
        out.write_text(text, encoding="utf-8")
    else:
        typer.echo(text)
