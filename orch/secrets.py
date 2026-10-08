"""API keys the owner types in (`orch key set`, or the web app's settings page).

Stored outside git: the command line keeps them in %APPDATA%/MAO/keys.json (~/.config/MAO on other
systems). On Windows each key is encrypted with DPAPI
(CryptProtectData, bound to the Windows account that runs the server), so the file is useless if it
is copied to another machine or account. Keys are never sent back to the browser, logged, or
written into the database; the page only learns whether a key is set and where it comes from.
An environment variable with the provider's name always wins over a key saved here.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
from pathlib import Path

_LOCK = threading.Lock()


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32

    def _blob(data: bytes) -> _Blob:
        buf = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    def _protect(data: bytes) -> bytes:
        out = _Blob()
        if not _crypt32.CryptProtectData(ctypes.byref(_blob(data)), "MAO web key", None, None, None, 0,
                                         ctypes.byref(out)):
            raise OSError("CryptProtectData failed")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(out.pbData)

    def _unprotect(data: bytes) -> bytes:
        out = _Blob()
        if not _crypt32.CryptUnprotectData(ctypes.byref(_blob(data)), None, None, None, None, 0,
                                           ctypes.byref(out)):
            raise OSError("CryptUnprotectData failed (saved by another Windows account?)")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(out.pbData)

    SCHEME = "dpapi"
else:  # pragma: no cover - the owner runs Windows; elsewhere rely on file permissions
    def _protect(data: bytes) -> bytes:
        return data

    def _unprotect(data: bytes) -> bytes:
        return data

    SCHEME = "plain"


class KeyStore:
    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        if sys.platform != "win32":
            os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def get(self, name: str) -> str | None:
        entry = self._read().get(name)
        if not entry:
            return None
        try:
            raw = base64.b64decode(entry["data"])
            return (_unprotect(raw) if entry.get("scheme") == "dpapi" else raw).decode("utf-8")
        except (OSError, KeyError, ValueError):
            return None

    def set(self, name: str, value: str) -> None:
        value = value.strip()
        with _LOCK:
            data = self._read()
            if value:
                data[name] = {"scheme": SCHEME, "data": base64.b64encode(_protect(value.encode("utf-8"))).decode()}
            else:
                data.pop(name, None)
            self._write(data)

    def has(self, name: str) -> bool:
        return name in self._read()


def default_path() -> Path:
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "MAO" / "keys.json"


def user_keys() -> KeyStore:
    """The command line's key store (shared by every project on this PC)."""
    return KeyStore(Path(os.environ.get("MAO_KEYS_FILE") or default_path()))
