"""Auth dependencies.

Two flavours:

* ``current_user`` reads ``Remote-User`` (set by Authelia at the reverse proxy)
  and validates it against the optional ALLOWED_USERS allowlist. Use this on
  every browser-facing route.

* ``require_api_key`` checks ``X-API-Key`` against the configured shared secret
  using a constant-time comparison. Use this on Home Assistant webhook routes.
"""

from __future__ import annotations

import secrets

from fastapi import Depends, Header, HTTPException, status

from shifter.config import Settings, get_settings


def current_user(
    remote_user: str | None = Header(default=None, alias="Remote-User"),
    settings: Settings = Depends(get_settings),
) -> str:
    if settings.dev_mode:
        return remote_user or settings.dev_user
    if not remote_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Remote-User header (request must come via authenticating proxy)",
        )
    allowed = settings.allowed_user_set
    if allowed and remote_user not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"User '{remote_user}' is not in ALLOWED_USERS",
        )
    return remote_user


def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    settings: Settings = Depends(get_settings),
) -> None:
    if settings.dev_mode:
        return
    if not x_api_key or not secrets.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key",
        )
