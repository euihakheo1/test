"""Process settings (env prefix ``JETTAE_``), ``.env`` loading and the startup check.

Three environments only: ``dev`` (default), ``test`` and ``prod``. Any other value of
``JETTAE_ENV`` is a configuration error, because a typo such as ``production`` would otherwise
silently run with development defaults (ephemeral JWT secret, SQLite, insecure cookies).

``.env`` loading (backend): ``JETTAE_ENV_FILE`` names the file to load; otherwise ``./.env``
in the current working directory is loaded when present. Values already present in the real
process environment always win over file values, so a deployment can override a checked-in
example without editing it. Loading writes into ``os.environ`` because several adapters (LLM,
OCR, data sources) read their variables directly from the environment.

:func:`require_valid_environment` is the single startup gate used by the API, worker, MCP
server and the DB / source commands. It raises :class:`SystemExit` with a readable message
instead of a stack trace, and never prints secret values.
"""

from __future__ import annotations

import os
import secrets
import warnings
from collections.abc import Mapping, MutableMapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_UPLOAD_TYPES = (".csv", ".txt", ".xlsx", ".pdf")
ENVIRONMENTS = ("dev", "test", "prod")
MIN_JWT_SECRET_BYTES = 32

# Values copied from documentation or examples. A secret containing one of these markers was
# not generated for this deployment.
_PLACEHOLDER_MARKERS = (
    "change-me",
    "changeme",
    "change_me",
    "replace-me",
    "replace_me",
    "replace-with",
    "placeholder",
    "example",
    "your-secret",
    "dummy",
    "not-a-secret",
    "insecure",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JETTAE_", env_file=None, extra="ignore", populate_by_name=True
    )

    env: str = "dev"  # dev | test | prod (validated; anything else is rejected)
    database_url: str = "sqlite:///./var/jettae.db"
    blob_dir: Path = Path("./var/blobs")
    migrations_dir: Path = REPO_ROOT / "migrations"
    ingest_entrypoint: str = "jettae.ingest.pipeline"

    # --- auth
    jwt_secret: str | None = None
    jwt_issuer: str = "jettae"
    jwt_audience: str = "jettae-api"
    access_token_ttl_s: int = 900
    refresh_token_ttl_s: int = 14 * 24 * 3600
    # Absolute lifetime of a browser session (refresh-token family). Rotation never extends a
    # session past this, so a stolen refresh cookie cannot keep a session alive forever.
    session_max_age_s: int = 30 * 24 * 3600
    argon2_time_cost: int = 3
    argon2_memory_kib: int = 65536
    argon2_parallelism: int = 4
    password_min_length: int = 10
    # A rotated refresh token presented again within this many seconds is answered with a new
    # access token (no new refresh token) instead of ending the session: tabs sharing one
    # cookie jar refresh at the same moment when their common access cookie expires. Later
    # reuse still ends the session (theft). 0 = no grace.
    refresh_reuse_grace_s: int = 20
    # throttling (per API process; 0 disables)
    auth_ip_per_minute: int = 30  # signup/login requests per client IP per minute
    # POST /auth/refresh per session per minute. Not per IP: behind a proxy or the Next.js
    # rewrite every browser can share one client IP, and failed logins of one client must not
    # make every other user's refresh fail.
    refresh_per_minute: int = 30
    login_max_failures: int = 5  # failed logins per email within login_lockout_s -> 429
    login_lockout_s: int = 900
    public_ip_per_minute: int = 30  # POST /public/due per client IP per minute

    # --- browser session cookies
    # None = follow the environment (Secure in prod, off in dev/test so http://localhost works).
    # Setting it to false in prod is rejected at startup.
    cookie_secure: bool | None = None

    # --- uploads
    max_upload_bytes: int = 20 * 1024 * 1024
    max_json_body_bytes: int = 2 * 1024 * 1024
    upload_extensions: Annotated[tuple[str, ...], NoDecode] = DEFAULT_UPLOAD_TYPES

    # --- worker
    worker_concurrency: int = 2
    worker_poll_s: float = 1.0
    job_lease_s: int = 60
    job_max_attempts: int = 5
    job_backoff_base_s: float = 2.0
    job_backoff_max_s: float = 300.0

    # --- http
    # Exact browser origins (scheme://host[:port]) allowed to call the API with credentials.
    # JETTAE_CORS_ORIGINS is the older name of the same setting.
    allowed_origins: Annotated[tuple[str, ...], NoDecode] = Field(
        default=(),
        validation_alias=AliasChoices(
            "allowed_origins", "JETTAE_ALLOWED_ORIGINS", "JETTAE_CORS_ORIGINS", "cors_origins"
        ),
    )
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    @field_validator("env", mode="before")
    @classmethod
    def _check_env(cls, v: object) -> object:
        if isinstance(v, str) and v not in ENVIRONMENTS:
            raise ValueError(f"JETTAE_ENV must be one of {', '.join(ENVIRONMENTS)} (got {v!r})")
        return v

    @field_validator("upload_extensions", mode="before")
    @classmethod
    def _split_ext(cls, v: object) -> object:
        if isinstance(v, str):
            return tuple(x.strip().lower() for x in v.split(",") if x.strip())
        return v

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str):
            v = tuple(x.strip() for x in v.split(",") if x.strip())
        if isinstance(v, list | tuple):
            out = tuple(str(x).rstrip("/") for x in v)
            for o in out:
                _check_origin(o)
            return out
        return v

    # ------------------------------------------------------------------ derived values
    @property
    def cors_origins(self) -> tuple[str, ...]:
        """Older name of :attr:`allowed_origins` (kept for callers written before the rename)."""
        return self.allowed_origins

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    @property
    def secure_cookies(self) -> bool:
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.is_prod

    def resolved_jwt_secret(self) -> str:
        """The signing secret. In ``prod`` it must be configured and strong; ``dev``/``test``
        fall back to an ephemeral per-process secret (tokens die with the process)."""
        if self.jwt_secret:
            if self.is_prod:
                problem = jwt_secret_problem(self.jwt_secret)
                if problem:
                    raise RuntimeError(problem)
            return self.jwt_secret
        if self.is_prod:
            raise RuntimeError("JETTAE_JWT_SECRET is required when JETTAE_ENV=prod")
        if not _EPHEMERAL:
            warnings.warn(
                "JETTAE_JWT_SECRET not set: using an ephemeral per-process secret (dev only)",
                stacklevel=2,
            )
            _EPHEMERAL.append(secrets.token_urlsafe(48))
        return _EPHEMERAL[0]


_EPHEMERAL: list[str] = []


def _check_origin(origin: str) -> None:
    """An origin is ``scheme://host[:port]``. Wildcards are refused because the API sends
    ``Access-Control-Allow-Credentials: true``; a wildcard would let any site read
    authenticated responses."""
    if origin == "*" or "*" in origin:
        raise ValueError("JETTAE_ALLOWED_ORIGINS must list exact origins, '*' is not allowed")
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.path or parts.query:
        raise ValueError(
            f"JETTAE_ALLOWED_ORIGINS entry {origin!r} must look like https://host[:port]"
        )


def get_settings() -> Settings:
    return Settings()


# ---------------------------------------------------------------------------- .env loading
class EnvFileError(RuntimeError):
    pass


def env_file_path(environ: Mapping[str, str] | None = None, cwd: Path | None = None) -> Path | None:
    """The file :func:`load_env` would read: ``JETTAE_ENV_FILE`` or ``./.env`` if present."""
    e = os.environ if environ is None else environ
    explicit = (e.get("JETTAE_ENV_FILE") or "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_absolute():
            p = (cwd or Path.cwd()) / p
        if not p.is_file():
            # An explicitly named file that does not exist is a deployment mistake; running
            # on defaults instead would hide it.
            raise EnvFileError(f"JETTAE_ENV_FILE points to a missing file: {p}")
        return p
    default = (cwd or Path.cwd()) / ".env"
    return default if default.is_file() else None


def load_env(
    environ: MutableMapping[str, str] | None = None, cwd: Path | None = None
) -> Path | None:
    """Load the ``.env`` file into ``environ`` (default ``os.environ``) without overriding
    variables that are already set. Returns the loaded path (or None)."""
    from dotenv import dotenv_values

    target: MutableMapping[str, str] = os.environ if environ is None else environ
    path = env_file_path(target, cwd)
    if path is None:
        return None
    for key, value in dotenv_values(path, interpolate=False).items():
        if value is None or key in target:
            continue
        target[key] = value
    return path


# ---------------------------------------------------------------------------- startup gate
COMPONENTS = ("api", "worker", "mcp", "db", "sources")
# Source commands that call the 법제처 DRF API and therefore need an own OC key in prod.
# ``sources:bpi2019`` downloads from 4TU and needs no key.
DRF_SOURCE_COMPONENTS = ("sources", "sources:ftc", "sources:law")


def jwt_secret_problem(secret: str | None) -> str | None:
    """Why ``secret`` is unfit for prod, or None. Never includes the secret itself."""
    if not secret:
        return "JETTAE_JWT_SECRET is required when JETTAE_ENV=prod"
    if len(secret.encode("utf-8")) < MIN_JWT_SECRET_BYTES:
        return f"JETTAE_JWT_SECRET must be at least {MIN_JWT_SECRET_BYTES} bytes in prod"
    low = secret.lower()
    if any(m in low for m in _PLACEHOLDER_MARKERS):
        return "JETTAE_JWT_SECRET looks like a placeholder value; generate a random secret"
    if len(set(secret)) < 8:
        return "JETTAE_JWT_SECRET has too few distinct characters; generate a random secret"
    return None


def _is_postgres(url: str | None) -> bool:
    return bool(url) and str(url).split(":", 1)[0].split("+", 1)[0] in ("postgresql", "postgres")


def _positive_finite(raw: str | None) -> bool:
    # Decimal, not float: float("1e400") is inf and float("Infinity") > 0, so an unlimited
    # budget would pass. Same rule as the LLM budget itself (llm.budget.budget_limit_problem).
    from jettae.llm.budget import budget_limit_problem

    try:
        d = Decimal((raw or "0").strip())
    except InvalidOperation:
        return False
    return d > 0 if budget_limit_problem(d) is None else False


def database_password_problem(url: str | None, variable: str) -> str | None:
    """Why the password in ``url`` is unfit for prod, or None. The example files ship a
    placeholder password; never includes the password itself."""
    if not url:
        return None
    try:
        password = make_url(url).password
    except ArgumentError:
        return f"{variable} is not a valid database URL"
    if password is None:
        return None
    low = str(password).lower()
    if any(m in low for m in _PLACEHOLDER_MARKERS):
        return f"{variable} contains a placeholder password; set the real database password"
    return None


def environment_problems(
    settings: Settings, component: str, environ: Mapping[str, str] | None = None
) -> list[str]:
    """Configuration errors for ``component`` under ``settings.env``. Empty = OK.

    LLM mode and local endpoint restrictions apply in every environment. Deployment secrets,
    HTTPS and PostgreSQL are required in ``prod``. Each component is checked
    for what it actually uses: a source download does not need the JWT secret, and the
    worker does not sign tokens."""
    e = os.environ if environ is None else environ
    mode = (e.get("JETTAE_LLM_MODE") or "offline").strip().lower()
    llm_problems: list[str] = []
    if component.split(":", 1)[0] in ("api", "worker", "mcp"):
        if mode not in ("offline", "replay", "live", "local"):
            llm_problems.append("JETTAE_LLM_MODE must be offline, replay, live or local")
        if mode == "local":
            from jettae.llm.vllm import local_configuration_problem

            problem = local_configuration_problem(e)
            if problem:
                llm_problems.append(problem)
            if settings.is_prod:
                problem = jwt_secret_problem(e.get("JETTAE_VLLM_API_KEY"))
                if problem:
                    llm_problems.append(problem.replace("JETTAE_JWT_SECRET", "JETTAE_VLLM_API_KEY"))
    if not settings.is_prod:
        return llm_problems
    problems: list[str] = llm_problems
    base = component.split(":", 1)[0]
    if base in ("api", "mcp"):
        p = jwt_secret_problem(settings.jwt_secret)
        if p:
            problems.append(p)
    if base in ("api", "worker", "mcp", "db"):
        if not _is_postgres(settings.database_url):
            problems.append(
                "JETTAE_DATABASE_URL must be a PostgreSQL URL in prod (postgresql+psycopg://...)"
            )
        p = database_password_problem(settings.database_url, "JETTAE_DATABASE_URL")
        if p:
            problems.append(p)
    if base == "api":
        if not settings.allowed_origins:
            problems.append("JETTAE_ALLOWED_ORIGINS must list the web origin(s) in prod")
        elif any(not o.startswith("https://") for o in settings.allowed_origins):
            problems.append("JETTAE_ALLOWED_ORIGINS entries must use https:// in prod")
        if settings.cookie_secure is False:
            problems.append("JETTAE_COOKIE_SECURE=false is not allowed in prod")
    if component in DRF_SOURCE_COMPONENTS:
        oc = (e.get("JETTAE_DRF_OC") or "").strip()
        # an unedited example value (``change-me-...``) is refused like a missing key
        if not oc or oc == "test" or any(m in oc.lower() for m in _PLACEHOLDER_MARKERS):
            problems.append(
                "JETTAE_DRF_OC must be your own law.go.kr OC in prod (the sample key 'test' is "
                "for development only)"
            )
    if base in ("api", "worker", "mcp"):
        mode = (e.get("JETTAE_LLM_MODE") or "offline").strip().lower()
        if mode == "live":
            if not _positive_finite(e.get("JETTAE_LLM_BUDGET_KRW")):
                problems.append(
                    "JETTAE_LLM_MODE=live requires JETTAE_LLM_BUDGET_KRW > 0 (finite, at most "
                    "10000000000) in prod"
                )
            if not _is_postgres(e.get("JETTAE_LLM_BUDGET_DB")):
                problems.append(
                    "JETTAE_LLM_MODE=live requires JETTAE_LLM_BUDGET_DB (shared PostgreSQL "
                    "budget) in prod, so every process reserves against one limit"
                )
            p = database_password_problem(e.get("JETTAE_LLM_BUDGET_DB"), "JETTAE_LLM_BUDGET_DB")
            if p:
                problems.append(p)
    return problems


def require_valid_environment(
    component: str,
    *,
    settings: Settings | None = None,
    cwd: Path | None = None,
    load_dotenv: bool = True,
) -> Settings:
    """Load ``.env`` into the process environment, build and check the settings for
    ``component``; exit with a readable message on any configuration error."""
    if component.split(":", 1)[0] not in COMPONENTS:
        raise ValueError(f"unknown component {component!r}")
    try:
        if load_dotenv:
            load_env(None, cwd)
    except (EnvFileError, OSError) as exc:
        raise SystemExit(f"[jettae {component}] configuration error: {exc}") from None
    raw_env = os.environ.get("JETTAE_ENV")
    if settings is None and raw_env is not None and raw_env not in ENVIRONMENTS:
        raise SystemExit(
            f"[jettae {component}] configuration error: JETTAE_ENV must be one of "
            f"{', '.join(ENVIRONMENTS)} (got {raw_env!r})"
        )
    if settings is None:
        try:
            settings = Settings()
        except ValidationError as exc:
            msgs = "; ".join(
                f"{'.'.join(str(x) for x in err['loc']) or 'settings'}: {err['msg']}"
                for err in exc.errors(include_input=False, include_url=False)
            )
            raise SystemExit(f"[jettae {component}] configuration error: {msgs}") from None
    problems = environment_problems(settings, component)
    if problems:
        lines = "".join(f"\n  - {p}" for p in problems)
        raise SystemExit(
            f"[jettae {component}] refusing to start with JETTAE_ENV={settings.env}:{lines}"
        )
    return settings


__all__ = [
    "COMPONENTS",
    "DEFAULT_UPLOAD_TYPES",
    "ENVIRONMENTS",
    "REPO_ROOT",
    "EnvFileError",
    "Settings",
    "database_password_problem",
    "environment_problems",
    "env_file_path",
    "get_settings",
    "jwt_secret_problem",
    "load_env",
    "require_valid_environment",
]
