"""Browser session storage (revision ``0005_auth_sessions``).

One ``auth_sessions`` row per login: its id is the refresh-token ``family_id`` and the ``sid``
claim of every access token issued in that session. Access tokens are checked against this
row on every request, so logout and refresh-token reuse detection end the session
immediately instead of when the short-lived access token expires. ``expires_at`` is the
absolute session lifetime; refresh rotation never extends it.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from jettae.db.orm import ID, Base, Timestamps, UtcDateTime, tenant_fk


class AuthSessionRow(Timestamps, Base):
    __tablename__ = "auth_sessions"
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ID, tenant_fk(), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(
        ID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    # logout | refresh_reuse | ... (diagnostics only; never shown to other tenants)
    revoked_reason: Mapped[str | None] = mapped_column(sa.String(40), nullable=True)
