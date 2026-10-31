"""Authenticated, confidential server-owned state for Responses history replay.

``agy2.`` contains Fernet-encrypted JSON binding model, account and original
upstream text/signature. Legacy readable ``agy1.`` blobs are deliberately refused.
The state key is independent of API credentials, whose holders are untrusted
with respect to the replay-state integrity boundary.
"""
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "agy2."
CALL_ID_PREFIX = "call_agy2_"
MAX_PLAINTEXT_BYTES = 4 * 1024 * 1024
MAX_BLOB_CHARS = 6 * 1024 * 1024


class StateKeyError(RuntimeError):
    """The private replay-state key cannot be safely initialized or used."""


def account_hash(email: Optional[str]) -> str:
    if not email:
        return ""
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:16]


def _validate_key(raw: bytes) -> bytes:
    try:
        value = raw.strip()
        if len(value) != 44:
            raise ValueError
        Fernet(value)
        return value
    except (TypeError, ValueError) as exc:
        raise StateKeyError("Invalid Responses state key configuration") from exc


def _read_private_key(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if path.is_symlink():
        raise StateKeyError("Responses state key must be a private regular file")
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 64:
            raise StateKeyError("Responses state key must be a private regular file")
        if os.name == "posix" and ((info.st_mode & 0o077) or info.st_uid != os.geteuid()):
            raise StateKeyError("Responses state key has unsafe ownership or permissions")
        return _validate_key(os.read(fd, 65))
    finally:
        os.close(fd)


def initialize_key() -> bytes:
    """Validate env key or atomically create/load a stable private file key.

    Link a fully written private temporary file into place, so concurrent workers
    cannot observe an empty or partial key. No key is derived from API passwords.
    """
    configured = os.getenv("RESPONSES_STATE_KEY", "")
    if configured:
        return _validate_key(configured.encode("ascii", errors="replace"))
    directory = Path(os.getenv("CREDENTIALS_DIR", "./creds"))
    path = directory / "responses-state.key"
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory_info = directory.stat()
        if os.name == "posix" and (directory_info.st_mode & 0o022):
            raise StateKeyError("Responses state directory must not be writable by other users")
        try:
            return _read_private_key(path)
        except FileNotFoundError:
            pass
        temporary = directory / (".responses-state-" + uuid.uuid4().hex + ".tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                key = Fernet.generate_key()
                os.write(fd, key)
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                pass  # another worker installed its complete key first
            return _read_private_key(path)
        finally:
            temporary.unlink(missing_ok=True)
    except StateKeyError:
        raise
    except (OSError, ValueError) as exc:
        raise StateKeyError("Cannot initialize private Responses state key") from exc


def encode(model: str, account_email: Optional[str], signature: str, text: str, *,
           call_id: Optional[str] = None, function_name: Optional[str] = None,
           arguments_hash: Optional[str] = None) -> str:
    owner = account_hash(account_email)
    if not model or not owner or not isinstance(signature, str) or not signature or not isinstance(text, str):
        raise ValueError("Replay state requires model, selected account and upstream signature")
    payload = {"v": 2, "m": model, "a": owner, "s": signature, "t": text}
    if call_id is not None:
        payload.update(k="function_call", id=call_id, n=function_name, g=arguments_hash)
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(raw) > MAX_PLAINTEXT_BYTES:
        raise ValueError("Responses replay state exceeds the supported size")
    return PREFIX + Fernet(initialize_key()).encrypt(raw).decode("ascii")


def decode(blob: Any) -> Optional[Dict[str, Any]]:
    """Authenticate before parsing; return None for invalid, legacy or foreign state.

    No decompression is performed, and encoded/decrypted sizes are bounded. A
    caller must turn None into an explicit invalid-state response, never fallback.
    """
    if not isinstance(blob, str) or not blob.startswith(PREFIX) or len(blob) > MAX_BLOB_CHARS:
        return None
    try:
        token = blob[len(PREFIX):].encode("ascii")
        decoded = base64.b64decode(token, altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(decoded) != token:
            return None
        raw = Fernet(initialize_key()).decrypt(token)
        if len(raw) > MAX_PLAINTEXT_BYTES:
            return None
        payload = json.loads(raw.decode("utf-8"))
    except (InvalidToken, UnicodeError, ValueError, TypeError, binascii.Error):
        return None
    if not isinstance(payload, dict) or payload.get("v") != 2:
        return None
    if not all(isinstance(payload.get(k), str) for k in ("m", "a", "s", "t")):
        return None
    if not payload["m"] or not payload["s"] or len(payload["a"]) != 16:
        return None
    if any(ch not in "0123456789abcdef" for ch in payload["a"]):
        return None
    if payload.get("k") == "function_call" and not all(
        isinstance(payload.get(k), str) and payload[k] for k in ("id", "n", "g")
    ):
        return None
    return payload
