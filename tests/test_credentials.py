"""Unit tests for credentials.py's keyring-backed storage, including the
read-only legacy-service-name fallback for credentials stored before the
tastytrade-mcp -> meicagent rename.

No real keyring backend touched — keyring.get_password/set_password/
delete_password are monkeypatched to an in-memory fake store.
"""
from __future__ import annotations

import sys
from pathlib import Path

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
    monkeypatch.setattr(credentials.keyring, "get_password", fake.get_password)
    monkeypatch.setattr(credentials.keyring, "set_password", fake.set_password)
    monkeypatch.setattr(credentials.keyring, "delete_password", fake.delete_password)
    return fake


def test_set_and_get_secret_roundtrip():
    credentials.set_secret(credentials.CLIENT_SECRET, "sekrit")
    assert credentials.get_secret(credentials.CLIENT_SECRET) == "sekrit"


def test_get_secret_missing_returns_none():
    assert credentials.get_secret(credentials.CLIENT_SECRET) is None


def test_get_secret_falls_back_to_legacy_service_name(fake_keyring):
    fake_keyring.store[(credentials._LEGACY_SERVICE_NAME, credentials._entry(credentials.CLIENT_SECRET))] = "legacy-value"
    assert credentials.get_secret(credentials.CLIENT_SECRET) == "legacy-value"


def test_get_secret_prefers_current_service_over_legacy(fake_keyring):
    fake_keyring.store[(credentials.SERVICE_NAME, credentials._entry(credentials.CLIENT_SECRET))] = "current-value"
    fake_keyring.store[(credentials._LEGACY_SERVICE_NAME, credentials._entry(credentials.CLIENT_SECRET))] = "legacy-value"
    assert credentials.get_secret(credentials.CLIENT_SECRET) == "current-value"


def test_set_secret_never_writes_to_legacy_service_name(fake_keyring):
    credentials.set_secret(credentials.CLIENT_SECRET, "x")
    assert (credentials._LEGACY_SERVICE_NAME, credentials._entry(credentials.CLIENT_SECRET)) not in fake_keyring.store


def test_secrets_present_false_when_required_missing():
    assert credentials.secrets_present() is False


def test_secrets_present_true_when_all_required_set():
    for key in credentials.REQUIRED_SECRETS:
        credentials.set_secret(key, "x")
    assert credentials.secrets_present() is True


def test_missing_secrets_lists_only_unset_required():
    credentials.set_secret(credentials.CLIENT_SECRET, "x")
    missing = credentials.missing_secrets()
    assert credentials.CLIENT_SECRET not in missing
    assert credentials.REFRESH_TOKEN in missing


def test_secrets_status_reports_each_key():
    credentials.set_secret(credentials.CLIENT_SECRET, "x")
    status = credentials.secrets_status()
    assert status[credentials.CLIENT_SECRET] is True
    assert status[credentials.REFRESH_TOKEN] is False


def test_delete_secret_removes_value():
    credentials.set_secret(credentials.CLIENT_SECRET, "x")
    credentials.delete_secret(credentials.CLIENT_SECRET)
    assert credentials.get_secret(credentials.CLIENT_SECRET) is None


def test_delete_secret_absent_is_a_noop():
    credentials.delete_secret(credentials.CLIENT_SECRET)  # must not raise


def test_get_secret_translates_no_keyring_error(monkeypatch):
    def _raise(service, entry):
        raise keyring.errors.NoKeyringError("no backend")
    monkeypatch.setattr(credentials.keyring, "get_password", _raise)
    with pytest.raises(credentials.CredentialError):
        credentials.get_secret(credentials.CLIENT_SECRET)


def test_set_secret_translates_keyring_error(monkeypatch):
    def _raise(service, entry, value):
        raise keyring.errors.KeyringError("write failed")
    monkeypatch.setattr(credentials.keyring, "set_password", _raise)
    with pytest.raises(credentials.CredentialError):
        credentials.set_secret(credentials.CLIENT_SECRET, "x")


def test_delete_secret_translates_keyring_error(monkeypatch):
    def _raise(service, entry):
        raise keyring.errors.KeyringError("delete failed")
    monkeypatch.setattr(credentials.keyring, "delete_password", _raise)
    with pytest.raises(credentials.CredentialError):
        credentials.delete_secret(credentials.CLIENT_SECRET)
