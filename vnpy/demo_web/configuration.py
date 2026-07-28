"""Current-operator encrypted configuration storage and activation."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
from threading import RLock
from time import time_ns
from typing import Any, Callable, Protocol


class ConfigurationError(RuntimeError):
    pass


class ConfigurationSecurityError(ConfigurationError):
    pass


class ConfigurationConflict(ConfigurationError):
    pass


class ConfigurationTestRequired(ConfigurationError):
    pass


class SecretProtector(Protocol):
    def protect(self, plaintext: bytes, *, context: bytes) -> bytes: ...

    def unprotect(self, ciphertext: bytes, *, context: bytes) -> bytes: ...


class CurrentUserDpapi:
    """Use Windows CurrentUser DPAPI with mandatory application entropy."""

    _UI_FORBIDDEN = 0x1

    def protect(self, plaintext: bytes, *, context: bytes) -> bytes:
        return self._crypt(plaintext, context=context, decrypt=False)

    def unprotect(self, ciphertext: bytes, *, context: bytes) -> bytes:
        return self._crypt(ciphertext, context=context, decrypt=True)

    def _crypt(self, value: bytes, *, context: bytes, decrypt: bool) -> bytes:
        if os.name != "nt":
            raise ConfigurationSecurityError("DPAPI_CURRENT_USER_UNAVAILABLE")

        class DataBlob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        def blob(data: bytes) -> tuple[DataBlob, Any]:
            buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
            return DataBlob(len(data), buffer), buffer

        input_blob, input_buffer = blob(value)
        entropy_blob, entropy_buffer = blob(context)
        output_blob = DataBlob()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
        try:
            if decrypt:
                ok = operation(
                    ctypes.byref(input_blob),
                    None,
                    ctypes.byref(entropy_blob),
                    None,
                    None,
                    self._UI_FORBIDDEN,
                    ctypes.byref(output_blob),
                )
            else:
                ok = operation(
                    ctypes.byref(input_blob),
                    None,
                    ctypes.byref(entropy_blob),
                    None,
                    None,
                    self._UI_FORBIDDEN,
                    ctypes.byref(output_blob),
                )
            if not ok:
                code = ctypes.get_last_error()
                error = "CONFIGURATION_OPERATOR_MISMATCH" if decrypt else "CONFIGURATION_ENCRYPTION_FAILED"
                raise ConfigurationSecurityError(f"{error}:{code}")
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            del input_buffer, entropy_buffer
            if output_blob.pbData:
                kernel32.LocalFree(output_blob.pbData)


class ConfigurationStore:
    """Atomic draft, receipt, encrypted-secret, active, and rollback store."""

    def __init__(
        self,
        root: Path,
        *,
        operator_identity: str,
        protector: SecretProtector | None = None,
        clock_ms: Callable[[], int] | None = None,
        campaign_active: Callable[[], bool] | None = None,
    ) -> None:
        if not operator_identity.strip():
            raise ConfigurationSecurityError("CONFIGURATION_OPERATOR_REQUIRED")
        self.root = Path(root)
        self.secret_directory = self.root / "secrets"
        self._draft_path = self.root / "draft.json"
        self._secret_path = self.secret_directory / "draft.bin"
        self._active_path = self.root / "active.json"
        self._versions = self.root / "versions"
        self._operator_identity = operator_identity
        self._context = sha256(("auto-trade-config:" + operator_identity).encode()).digest()
        self._protector = protector or CurrentUserDpapi()
        self._clock_ms = clock_ms or (lambda: time_ns() // 1_000_000)
        self._campaign_active = campaign_active or (lambda: False)
        self._lock = RLock()
        self.root.mkdir(parents=True, exist_ok=True)
        self._versions.mkdir(parents=True, exist_ok=True)
        _secure_secret_directory(self.secret_directory)

    def read_draft(self) -> dict[str, Any]:
        with self._lock:
            draft = self._load_draft()
            return self._public_draft(draft, self._load_secrets())

    def read_active(self) -> dict[str, Any]:
        with self._lock:
            if not self._active_path.exists():
                return {"state": "unconfigured", "version": 0}
            return _read_json(self._active_path)

    def read_secret(self, key: str) -> str | None:
        with self._lock:
            return self._load_secrets().get(key)

    def update_draft(
        self,
        *,
        expected_revision: int,
        sections: dict[str, dict[str, Any]],
        secret_updates: dict[str, str] | None = None,
        clear_secrets: list[str] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._assert_mutable()
            draft = self._load_draft()
            if draft["revision"] != expected_revision:
                raise ConfigurationConflict("CONFIGURATION_REVISION_CONFLICT")
            secrets = self._load_secrets()
            changed: set[str] = set(draft.get("changed_sections", []))
            for section, value in sections.items():
                if not isinstance(value, dict):
                    raise ConfigurationError("CONFIGURATION_SECTION_INVALID")
                if draft["sections"].get(section) != value:
                    draft["sections"][section] = value
                    changed.add(section)
            for key, value in (secret_updates or {}).items():
                if not key or not isinstance(value, str) or not value:
                    raise ConfigurationSecurityError("CONFIGURATION_SECRET_INVALID")
                secrets[key] = value
                draft.setdefault("known_secret_keys", [])
                if key not in draft["known_secret_keys"]:
                    draft["known_secret_keys"].append(key)
                changed.add(key.split(".", 1)[0])
            for key in clear_secrets or []:
                secrets.pop(key, None)
                draft.setdefault("known_secret_keys", [])
                if key not in draft["known_secret_keys"]:
                    draft["known_secret_keys"].append(key)
                changed.add(key.split(".", 1)[0])
            draft["revision"] += 1
            draft["updated_at_ms"] = self._clock_ms()
            draft["changed_sections"] = sorted(changed)
            draft["test_receipts"] = {
                key: receipt
                for key, receipt in draft.get("test_receipts", {}).items()
                if key not in changed
            }
            self._write_secrets(secrets)
            _atomic_json(self._draft_path, draft)
            return self._public_draft(draft, secrets)

    def record_section_test(
        self,
        section: str,
        *,
        expected_revision: int,
        passed: bool,
        code: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            draft = self._load_draft()
            if draft["revision"] != expected_revision:
                raise ConfigurationConflict("CONFIGURATION_REVISION_CONFLICT")
            if section not in draft.get("changed_sections", []):
                raise ConfigurationError("CONFIGURATION_SECTION_UNCHANGED")
            now = self._clock_ms()
            receipt = {
                "section": section,
                "revision": expected_revision,
                "section_digest": self._section_digest(draft, self._load_secrets(), section),
                "passed": passed,
                "code": code if not passed else None,
                "checked_at_ms": now,
                "expires_at_ms": now + 600_000,
            }
            draft.setdefault("test_receipts", {})[section] = receipt
            _atomic_json(self._draft_path, draft)
            return dict(receipt)

    def activate(
        self,
        *,
        expected_revision: int,
        health_check: Callable[[dict[str, Any]], bool],
    ) -> dict[str, Any]:
        with self._lock:
            self._assert_mutable()
            draft = self._load_draft()
            if draft["revision"] != expected_revision:
                raise ConfigurationConflict("CONFIGURATION_REVISION_CONFLICT")
            now = self._clock_ms()
            secrets = self._load_secrets()
            for section in draft.get("changed_sections", []):
                receipt = draft.get("test_receipts", {}).get(section)
                if not receipt or not receipt.get("passed"):
                    raise ConfigurationTestRequired("CONFIGURATION_TEST_REQUIRED")
                if receipt["expires_at_ms"] < now:
                    raise ConfigurationTestRequired("CONFIGURATION_TEST_EXPIRED")
                if receipt["section_digest"] != self._section_digest(draft, secrets, section):
                    raise ConfigurationTestRequired("CONFIGURATION_TEST_STALE")
            previous = self.read_active()
            version = int(previous.get("version", 0)) + 1
            candidate = {
                "state": "active",
                "version": version,
                "draft_revision": draft["revision"],
                "configuration_digest": _digest_json(
                    {"sections": draft["sections"], "secret_digests": self._secret_digests(secrets)}
                ),
                "sections": draft["sections"],
                "activated_at_ms": now,
                "previous_version": int(previous.get("version", 0)),
                "operator_identity_digest": "sha256:" + sha256(self._operator_identity.encode()).hexdigest(),
            }
            _atomic_json(self._versions / f"{version}.json", candidate)
            if not health_check(dict(candidate)):
                return {
                    "state": "activation_failed",
                    "failed_version": version,
                    "restored_version": int(previous.get("version", 0)),
                    "revision": draft["revision"],
                }
            _atomic_json(self._active_path, candidate)
            draft["base_active_version"] = version
            draft["changed_sections"] = []
            draft["test_receipts"] = {}
            _atomic_json(self._draft_path, draft)
            return dict(candidate)

    def _assert_mutable(self) -> None:
        if self._campaign_active():
            raise ConfigurationConflict("CONFIGURATION_CAMPAIGN_ACTIVE")

    def _load_draft(self) -> dict[str, Any]:
        if self._draft_path.exists():
            return _read_json(self._draft_path)
        return {
            "draft_id": "current",
            "revision": 0,
            "base_active_version": 0,
            "sections": {},
            "changed_sections": [],
            "test_receipts": {},
            "known_secret_keys": [],
            "updated_at_ms": self._clock_ms(),
        }

    def _load_secrets(self) -> dict[str, str]:
        if not self._secret_path.exists():
            return {}
        plaintext = self._protector.unprotect(self._secret_path.read_bytes(), context=self._context)
        try:
            value = json.loads(plaintext)
        finally:
            del plaintext
        if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
            raise ConfigurationSecurityError("CONFIGURATION_SECRET_PAYLOAD_INVALID")
        return value

    def _write_secrets(self, secrets: dict[str, str]) -> None:
        plaintext = json.dumps(secrets, sort_keys=True, separators=(",", ":")).encode()
        try:
            encrypted = self._protector.protect(plaintext, context=self._context)
        finally:
            del plaintext
        _atomic_bytes(self._secret_path, encrypted)

    def _section_digest(self, draft: dict[str, Any], secrets: dict[str, str], section: str) -> str:
        return _digest_json(
            {
                "section": draft["sections"].get(section),
                "secrets": {
                    key: "sha256:" + sha256(value.encode()).hexdigest()
                    for key, value in sorted(secrets.items())
                    if key.split(".", 1)[0] == section
                },
            }
        )

    @staticmethod
    def _secret_digests(secrets: dict[str, str]) -> dict[str, str]:
        return {key: "sha256:" + sha256(value.encode()).hexdigest() for key, value in sorted(secrets.items())}

    @staticmethod
    def _public_draft(draft: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
        return {
            "draft_id": draft["draft_id"],
            "revision": draft["revision"],
            "base_active_version": draft["base_active_version"],
            "sections": draft["sections"],
            "changed_sections": draft.get("changed_sections", []),
            "test_receipts": draft.get("test_receipts", {}),
            "secret_status": {
                key: {"configured": key in secrets}
                for key in sorted(set(draft.get("known_secret_keys", [])) | set(secrets))
            },
            "updated_at_ms": draft["updated_at_ms"],
        }


def _secure_secret_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    if os.name == "nt":
        identity = subprocess.run(["whoami"], check=True, capture_output=True, text=True).stdout.strip()
        subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{identity}:(OI)(CI)F",
                "/grant:r",
                "*S-1-5-18:(OI)(CI)F",
            ],
            check=True,
            capture_output=True,
            text=True,
        )


def assert_secret_directory_secure(path: Path) -> None:
    if not path.is_dir():
        raise ConfigurationSecurityError("CONFIGURATION_SECRET_DIRECTORY_MISSING")
    if os.name != "nt":
        if path.stat().st_mode & 0o077:
            raise ConfigurationSecurityError("CONFIGURATION_SECRET_ACL_INSECURE")
        return
    sddl = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "& { param($target) (Get-Acl -LiteralPath $target).Sddl }",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if any(marker in sddl for marker in (";;;WD)", ";;;BU)", ";;;AU)")):
        raise ConfigurationSecurityError("CONFIGURATION_SECRET_ACL_INSECURE")


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ConfigurationError("CONFIGURATION_STATE_INVALID")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_bytes(path, json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def _atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("xb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def _digest_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + sha256(encoded).hexdigest()
