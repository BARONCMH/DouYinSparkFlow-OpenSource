"""Task policy owned by this project rather than inherited from the adapter."""

from __future__ import annotations

import os


DEFAULT_ACCOUNT_TIMEOUT_SECONDS = 600


def account_timeout_seconds(raw: object = None) -> int:
    """Return the per-account timeout with a safe, bounded configuration.

    The timeout is deliberately account-scoped.  A slow or broken account
    cannot consume the entire run window for all following accounts.
    """
    value = raw
    if value is None or str(value).strip() == "":
        value = os.getenv("PANEL_ACCOUNT_TIMEOUT_SECONDS", str(DEFAULT_ACCOUNT_TIMEOUT_SECONDS))
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        parsed = DEFAULT_ACCOUNT_TIMEOUT_SECONDS
    return max(1, min(parsed, 24 * 60 * 60))
