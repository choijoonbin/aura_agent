import base64
import os

import pytest

from dwp_agent.key_provider import (
    KeyProviderConfigurationError,
    LocalFileKeyProvider,
    LocalInlineKeyProvider,
    VersionedKeyMaterial,
    load_versioned_key_material,
    resolve_key_provider,
)


def key(byte: int) -> str:
    return base64.b64encode(bytes([byte]) * 32).decode("ascii")


def test_local_inline_provider_is_the_explicit_local_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DWP_ENVIRONMENT", "local")
    monkeypatch.delenv("DWP_AGENT_KEY_PROVIDER", raising=False)
    monkeypatch.setenv("DWP_AGENT_DATA_KEY", key(1))
    monkeypatch.setenv("DWP_AGENT_DATA_KEY_VERSION", "local-v1")

    provider = resolve_key_provider()
    material = load_versioned_key_material(provider)

    assert isinstance(provider, LocalInlineKeyProvider)
    assert material.provider_id == "local-inline"
    assert material.active_version == "local-v1"
    assert "active_key" not in repr(material)


def test_local_provider_is_rejected_in_shared_environments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DWP_ENVIRONMENT", "qa")
    monkeypatch.setenv("DWP_AGENT_DATA_KEY", key(1))

    with pytest.raises(KeyProviderConfigurationError, match="managed"):
        load_versioned_key_material(LocalInlineKeyProvider())


def test_shared_environment_requires_an_available_managed_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DWP_ENVIRONMENT", "production")
    monkeypatch.setenv("DWP_AGENT_KEY_PROVIDER", "azure-key-vault")
    monkeypatch.setenv("DWP_AGENT_KEY_REFERENCE", "https://vault.example/keys/agent")

    with pytest.raises(KeyProviderConfigurationError, match="adapter is unavailable"):
        resolve_key_provider()


def test_local_file_provider_reads_only_owner_private_files_below_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    root = tmp_path / ".dev-runtime" / "keys"
    active = root / "agent" / "payload" / "local-v2.key"
    previous = root / "agent" / "payload" / "local-v1.key"
    active.parent.mkdir(parents=True)
    active.write_text(key(2), encoding="utf-8")
    previous.write_text(key(1), encoding="utf-8")
    os.chmod(active, 0o600)
    os.chmod(previous, 0o600)
    monkeypatch.setenv("DWP_ENVIRONMENT", "local")
    monkeypatch.setenv("DWP_AGENT_LOCAL_KEY_ROOT", str(root))
    monkeypatch.setenv("DWP_AGENT_DATA_KEY_FILE", "agent/payload/local-v2.key")
    monkeypatch.setenv("DWP_AGENT_DATA_KEY_VERSION", "local-v2")
    monkeypatch.setenv(
        "DWP_AGENT_PREVIOUS_DATA_KEY_FILES",
        '{"local-v1":"agent/payload/local-v1.key"}',
    )

    material = LocalFileKeyProvider().load()

    assert material.active_key == key(2)
    assert material.previous_keys == {"local-v1": key(1)}


def test_local_file_provider_rejects_escape_and_broad_permissions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    root = tmp_path / ".dev-runtime" / "keys"
    root.mkdir(parents=True)
    outside = tmp_path / "outside.key"
    outside.write_text(key(1), encoding="utf-8")
    os.chmod(outside, 0o600)
    monkeypatch.setenv("DWP_ENVIRONMENT", "local")
    monkeypatch.setenv("DWP_AGENT_LOCAL_KEY_ROOT", str(root))
    monkeypatch.setenv("DWP_AGENT_DATA_KEY_FILE", "../../outside.key")

    with pytest.raises(KeyProviderConfigurationError, match="outside"):
        LocalFileKeyProvider().load()

    inside = root / "agent.key"
    inside.write_text(key(1), encoding="utf-8")
    os.chmod(inside, 0o644)
    monkeypatch.setenv("DWP_AGENT_DATA_KEY_FILE", "agent.key")
    with pytest.raises(KeyProviderConfigurationError, match="0600"):
        LocalFileKeyProvider().load()


def test_local_file_provider_rejects_absolute_paths_and_symbolic_links(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    root = tmp_path / ".dev-runtime" / "keys"
    root.mkdir(parents=True)
    target = root / "target.key"
    target.write_text(key(1), encoding="utf-8")
    os.chmod(target, 0o600)
    link = root / "agent.key"
    link.symlink_to(target)
    monkeypatch.setenv("DWP_ENVIRONMENT", "local")
    monkeypatch.setenv("DWP_AGENT_LOCAL_KEY_ROOT", str(root))
    monkeypatch.setenv("DWP_AGENT_DATA_KEY_FILE", str(target))

    with pytest.raises(KeyProviderConfigurationError, match="outside"):
        LocalFileKeyProvider().load()

    monkeypatch.setenv("DWP_AGENT_DATA_KEY_FILE", "agent.key")
    with pytest.raises(KeyProviderConfigurationError, match="Symbolic links"):
        LocalFileKeyProvider().load()


def test_local_provider_rejects_unknown_environments_and_duplicate_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DWP_ENVIRONMENT", "customer-live")
    with pytest.raises(KeyProviderConfigurationError, match="DWP_ENVIRONMENT"):
        resolve_key_provider()

    monkeypatch.setenv("DWP_ENVIRONMENT", "local")
    monkeypatch.setenv("DWP_AGENT_DATA_KEY", key(1))
    monkeypatch.setenv("DWP_AGENT_DATA_KEY_VERSION", "local-v1")
    monkeypatch.setenv("DWP_AGENT_PREVIOUS_DATA_KEYS", '{"local-v1":"' + key(2) + '"}')
    with pytest.raises(KeyProviderConfigurationError, match="duplicated"):
        LocalInlineKeyProvider().load()


class ManagedTestKeyProvider:
    provider_id = "managed-test"
    managed = True

    def load(self) -> VersionedKeyMaterial:
        return VersionedKeyMaterial(
            provider_id=self.provider_id,
            active_version="managed-v1",
            active_key=key(9),
        )


def test_managed_provider_contract_supports_shared_environment_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DWP_ENVIRONMENT", "qa")

    material = load_versioned_key_material(ManagedTestKeyProvider())

    assert material.provider_id == "managed-test"
    assert material.active_version == "managed-v1"
