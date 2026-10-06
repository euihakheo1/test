"""Process wiring for the agent CLI and the MCP server: runtime + server-side tenant resolution.

The tenant is always derived from an API token (``jtk_...``) or access token through
:class:`jettae.api.auth.AuthService`; a tenant id given by a client is never used.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jettae.agents.stores import AgentStore
from jettae.api.auth import AuthService, Principal
from jettae.api.errors import ApiError
from jettae.db.config import Settings
from jettae.db.runtime import Runtime
from jettae.domain.dates import utc_now


class AuthFailed(PermissionError):
    pass


@dataclass
class AgentSession:
    runtime: Runtime
    auth: AuthService
    store: AgentStore

    @classmethod
    def open(
        cls,
        settings: Settings | None = None,
        *,
        clock: Callable[[], datetime] = utc_now,
        runtime: Runtime | None = None,
        store: AgentStore | None = None,
    ) -> AgentSession:
        rt = runtime or Runtime.build(settings or Settings(), clock=clock)
        return cls(rt, AuthService(rt.db, rt.settings, clock=rt.clock), store or store_from_env())

    def principal(self, token: str | None) -> Principal:
        if not token:
            raise AuthFailed("an API token is required (JETTAE_API_TOKEN or --token)")
        try:
            return self.auth.authenticate(token)
        except ApiError as e:
            raise AuthFailed(f"{e.code}: {e}") from e

    def close(self) -> None:
        self.runtime.close()


def store_from_env() -> AgentStore:
    return AgentStore(Path(os.environ.get("JETTAE_AGENT_DIR") or "var/agent"))
