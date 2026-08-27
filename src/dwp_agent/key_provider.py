from __future__ import annotations

import base64
import binascii
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Protocol


class KeyProviderConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class VersionedKeyMaterial:
    provider_id: str
    active_version: str
    active_key: str = field(repr=False)
    previous_keys: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "previous_keys", MappingProxyType(dict(self.previous_keys)))


class KeyProvider(Protocol):
    provider_id: str
    managed: bool

    def load(self) -> VersionedKeyMaterial:
        ...


@dataclass(frozen=True)
class LocalInlineKeyProvider:
    provider_id: str = "local-inline"
    managed: bool = False

    def load(self) -> VersionedKeyMaterial:
        _require_local_provider(self.provider_id)
        active_key = os.getenv("DWP_AGENT_DATA_KEY", "").strip()
        if not active_key:
            raise KeyProviderConfigurationError("Agent local data encryption key is required.")
        previous_keys = _string_map(
            os.getenv("DWP_AGENT_PREVIOUS_DATA_KEYS", "{}"),
            "Agent previous data keys",
        )
        active_version = _active_version()
        if active_version in previous_keys:
            raise KeyProviderConfigurationError(
                "Agent active key version is duplicated in previous keys."
            )
        return VersionedKeyMaterial(
            provider_id=self.provider_id,
            active_version=active_version,
            active_key=_validated_key(active_key),
            previous_keys={
                version: _validated_key(value) for version, value in previous_keys.items()
            },
        )


@dataclass(frozen=True)
class LocalFileKeyProvider:
    provider_id: str = "local-file"
    managed: bool = False

    def load(self) -> VersionedKeyMaterial:
        _require_local_provider(self.provider_id)
        root = Path(
            os.getenv("DWP_AGENT_LOCAL_KEY_ROOT", ".dev-runtime/keys").strip()
            or ".dev-runtime/keys"
        ).expanduser()
        active_file = os.getenv("DWP_AGENT_DATA_KEY_FILE", "").strip()
        if not active_file:
            raise KeyProviderConfigurationError("DWP_AGENT_DATA_KEY_FILE is required.")
        previous_files = _string_map(
            os.getenv("DWP_AGENT_PREVIOUS_DATA_KEY_FILES", "{}"),
            "Agent previous data key files",
        )
        active_version = _active_version()
        if active_version in previous_files:
            raise KeyProviderConfigurationError(
                "Agent active key version is duplicated in previous key files."
            )
        return VersionedKeyMaterial(
            provider_id=self.provider_id,
            active_version=active_version,
            active_key=_read_local_key(root, active_file),
            previous_keys={
                version: _read_local_key(root, file_name)
                for version, file_name in previous_files.items()
            },
        )


def load_versioned_key_material(
    provider: KeyProvider | None = None,
) -> VersionedKeyMaterial:
    selected = provider or resolve_key_provider()
    environment = _known_environment()
    if environment != "local" and not selected.managed:
        raise KeyProviderConfigurationError(
            "Shared environments require a managed Agent key provider."
        )
    material = selected.load()
    if material.provider_id != selected.provider_id:
        raise KeyProviderConfigurationError("Agent key provider identity is inconsistent.")
    return material


def resolve_key_provider() -> KeyProvider:
    environment = _known_environment()
    configured = os.getenv("DWP_AGENT_KEY_PROVIDER", "").strip().lower()
    provider_id = configured or ("local-inline" if environment == "local" else "")
    if provider_id == "local-inline":
        return LocalInlineKeyProvider()
    if provider_id == "local-file":
        return LocalFileKeyProvider()
    if not provider_id:
        raise KeyProviderConfigurationError(
            "DWP_AGENT_KEY_PROVIDER is required outside the local environment."
        )
    raise KeyProviderConfigurationError(
        "The configured managed Agent key provider adapter is unavailable."
    )


def normalized_environment() -> str:
    value = os.getenv("DWP_ENVIRONMENT", "local").strip().lower()
    aliases = {
        "development": "dev",
        "production": "prod",
        "quality": "qa",
        "staging": "qa",
    }
    return aliases.get(value, value or "local")


def _known_environment() -> str:
    environment = normalized_environment()
    if environment not in {"local", "dev", "qa", "prod"}:
        raise KeyProviderConfigurationError("DWP_ENVIRONMENT is not recognized.")
    return environment


def _require_local_provider(provider_id: str) -> None:
    if normalized_environment() != "local":
        raise KeyProviderConfigurationError(
            f"{provider_id} is permitted only in the local environment."
        )


def _active_version() -> str:
    return os.getenv("DWP_AGENT_DATA_KEY_VERSION", "local-v1").strip() or "local-v1"


def _string_map(raw_value: str, label: str) -> dict[str, str]:
    try:
        parsed = json.loads(raw_value.strip() or "{}")
    except json.JSONDecodeError as error:
        raise KeyProviderConfigurationError(f"{label} must be a JSON object.") from error
    if not isinstance(parsed, dict) or any(
        not isinstance(version, str) or not isinstance(value, str)
        for version, value in parsed.items()
    ):
        raise KeyProviderConfigurationError(f"{label} must be a string map.")
    return parsed


def _read_local_key(root: Path, configured_path: str) -> str:
    if root.is_symlink() or not root.is_dir():
        raise KeyProviderConfigurationError(
            "Agent local key root must be a non-symbolic directory."
        )
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        raise KeyProviderConfigurationError("Agent local key root is unavailable.") from error
    candidate = Path(configured_path).expanduser()
    if candidate.is_absolute() or ".." in candidate.parts:
        raise KeyProviderConfigurationError(
            "Agent local key file is outside the configured root."
        )
    current = resolved_root
    for component in candidate.parts:
        current = current / component
        if current.is_symlink():
            raise KeyProviderConfigurationError(
                "Symbolic links are not allowed in Agent local key paths."
            )
    candidate = resolved_root / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise KeyProviderConfigurationError("Agent local key file is unavailable.") from error
    if not resolved.is_relative_to(resolved_root) or not resolved.is_file():
        raise KeyProviderConfigurationError("Agent local key file is outside the configured root.")
    try:
        metadata = resolved.stat()
        mode = stat.S_IMODE(metadata.st_mode)
        value = resolved.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise KeyProviderConfigurationError("Agent local key file cannot be read.") from error
    if mode != 0o600:
        raise KeyProviderConfigurationError("Agent local key file permissions must be 0600.")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise KeyProviderConfigurationError(
            "Agent local key file owner must match the runtime user."
        )
    if not value:
        raise KeyProviderConfigurationError("Agent local key file is empty.")
    return _validated_key(value)


def _validated_key(value: str) -> str:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise KeyProviderConfigurationError(
            "Agent local key is not valid base64."
        ) from error
    if len(decoded) != 32:
        raise KeyProviderConfigurationError(
            "Agent local key must contain exactly 32 bytes."
        )
    return value
