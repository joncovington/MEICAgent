"""Wiring tests for MEICAgent's credentials shim over cherrypit-core.

The exhaustive keyring behavior (prefix entries, error translation, delete idempotency, etc.) lives in
cherrypit-core's own test suite. Here we verify only that MEIC wires the shared CredentialStore with
the right service name + legacy fallback and preserves the module-level API existing call sites import.
No real keyring backend is touched — keyring's password functions are monkeypatched to an in-memory
fake (patched globally, since the keyring calls now happen inside cherrypit-core).
"""
from __future__ import annotations

import sys
from pathlib import Path

import keyring
import keyring.errors
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import credentials


class _FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, entry):
        return self.store.get((service, entry))

    def set_password(self, service, entry, value):
        self.store[(service, entry)] = value

    def delete_password(self, service, entry):
        key = (service, entry)
        if key not in self.store:
            raise keyring.errors.PasswordDeleteError("not found")
        del self.store[key]


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch):
    fake = _FakeKeyring()
    monkeypatch.setattr(keyring, "get_password", fake.get_password)
    monkeypatch.setattr(keyring, "set_password", fake.set_password)
    monkeypatch.setattr(keyring, "delete_password", fake.delete_password)
    return fake


def test_store_is_configured_for_meic():
    assert credentials.store.service_name == "meicagent"
    assert credentials._LEGACY_SERVICE_NAME in credentials.store.legacy_service_names


def test_public_api_preserved():
    for name in ("get_secret", "set_secret", "delete_secret",
                 "secrets_present", "missing_secrets", "secrets_status"):
        assert callable(getattr(credentials, name)), name
    for const in ("CLIENT_SECRET", "REFRESH_TOKEN", "ACCOUNT_NUMBER",
                  "REQUIRED_SECRETS", "ALL_SECRETS", "CredentialError"):
        assert hasattr(credentials, const), const


def test_set_and_get_secret_roundtrip(fake_keyring):
    credentials.set_secret(credentials.CLIENT_SECRET, "sekrit")
    assert credentials.get_secret(credentials.CLIENT_SECRET) == "sekrit"
    assert fake_keyring.store[("meicagent", "production:client_secret")] == "sekrit"


def test_get_secret_falls_back_to_legacy_service_name(fake_keyring):
    fake_keyring.store[("tastytrade-mcp", "production:refresh_token")] = "legacy-value"
    assert credentials.get_secret(credentials.REFRESH_TOKEN) == "legacy-value"


def test_set_secret_never_writes_to_legacy_service_name(fake_keyring):
    fake_keyring.store[("tastytrade-mcp", "production:refresh_token")] = "legacy-value"
    credentials.set_secret(credentials.REFRESH_TOKEN, "new-value")
    assert fake_keyring.store[("meicagent", "production:refresh_token")] == "new-value"
    assert fake_keyring.store[("tastytrade-mcp", "production:refresh_token")] == "legacy-value"


def test_present_missing_and_status(fake_keyring):
    assert credentials.secrets_present() is False
    credentials.set_secret(credentials.CLIENT_SECRET, "x")
    assert credentials.REFRESH_TOKEN in credentials.missing_secrets()
    status = credentials.secrets_status()
    assert status[credentials.CLIENT_SECRET] is True
    assert status[credentials.REFRESH_TOKEN] is False


def test_delete_secret_absent_is_a_noop(fake_keyring):
    credentials.delete_secret(credentials.CLIENT_SECRET)  # must not raise


def test_get_secret_translates_no_keyring_error(monkeypatch):
    def _raise(*_a, **_k):
        raise keyring.errors.NoKeyringError("no backend")

    monkeypatch.setattr(keyring, "get_password", _raise)
    with pytest.raises(credentials.CredentialError):
        credentials.get_secret(credentials.CLIENT_SECRET)
