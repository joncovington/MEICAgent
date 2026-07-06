"""Keyring-backed credential storage for tastytrade OAuth."""

from __future__ import annotations

import keyring
import keyring.errors

SERVICE_NAME = "meicagent"
_LEGACY_SERVICE_NAME = "tastytrade-mcp"  # read-only fallback for credentials stored before the rename

CLIENT_SECRET = "client_secret"
REFRESH_TOKEN = "refresh_token"
ACCOUNT_NUMBER = "account_number"

REQUIRED_SECRETS = (CLIENT_SECRET, REFRESH_TOKEN)
ALL_SECRETS = (CLIENT_SECRET, REFRESH_TOKEN, ACCOUNT_NUMBER)

_PREFIX = "production"


class CredentialError(RuntimeError):
    pass


def _entry(key: str) -> str:
    return f"{_PREFIX}:{key}"


def get_secret(key: str) -> str | None:
    try:
        value = keyring.get_password(SERVICE_NAME, _entry(key))
    except keyring.errors.NoKeyringError as exc:
        raise CredentialError("No keyring backend available.") from exc
    except keyring.errors.KeyringError as exc:
        raise CredentialError(f"Keyring read failed: {exc}") from exc
    if value is not None:
        return value
    # Fall back to the pre-rename service name so existing stored credentials
    # keep working without forcing a re-entry via secrets_set. Never written
    # to going forward — set_secret only writes under SERVICE_NAME.
    try:
        return keyring.get_password(_LEGACY_SERVICE_NAME, _entry(key))
    except keyring.errors.KeyringError:
        return None


def secrets_present() -> bool:
    return all(get_secret(k) for k in REQUIRED_SECRETS)


def missing_secrets() -> list[str]:
    return [k for k in REQUIRED_SECRETS if not get_secret(k)]


def set_secret(key: str, value: str) -> None:
    try:
        keyring.set_password(SERVICE_NAME, _entry(key), value)
    except keyring.errors.NoKeyringError as exc:
        raise CredentialError("No keyring backend available.") from exc
    except keyring.errors.KeyringError as exc:
        raise CredentialError(f"Keyring write failed: {exc}") from exc


def delete_secret(key: str) -> None:
    try:
        keyring.delete_password(SERVICE_NAME, _entry(key))
    except keyring.errors.PasswordDeleteError:
        pass  # already absent
    except keyring.errors.KeyringError as exc:
        raise CredentialError(f"Keyring delete failed: {exc}") from exc


def secrets_status() -> dict[str, bool]:
    """Return {key: is_set} for all known secrets."""
    return {k: bool(get_secret(k)) for k in ALL_SECRETS}
