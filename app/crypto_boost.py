"""Fast AES for Telethon on Android/Termux.

Telethon decrypts every downloaded chunk with AES-IGE. Without `cryptg` it tries the
system OpenSSL, but on Android `ctypes.util.find_library` usually cannot locate it and
Telethon falls back to pure Python, which is far too slow for video. Here we load
Termux's libcrypto directly and hand it to Telethon.
"""
from __future__ import annotations

import ctypes
import logging
import os

log = logging.getLogger(__name__)


def _candidates() -> list[str]:
    prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
    names = ["libcrypto.so", "libcrypto.so.3", "libcrypto.so.1.1", "libssl.so", "libssl.so.3"]
    return [os.path.join(prefix, "lib", n) for n in names] + names


def install() -> str:
    """Returns which AES backend Telethon will use."""
    try:
        import cryptg  # noqa: F401
        return "cryptg"
    except ImportError:
        pass
    from telethon.crypto import libssl

    if libssl.decrypt_ige:
        return "libssl"
    lib = None
    for path in _candidates():
        try:
            lib = ctypes.CDLL(path)
            lib.AES_ige_encrypt  # noqa: B018 - make sure the symbol exists
            break
        except (OSError, AttributeError):
            lib = None
    if lib is None:
        log.warning("OpenSSL not found - video decryption will be slow (pkg install openssl)")
        return "python"

    class AES_KEY(ctypes.Structure):
        _fields_ = [("rd_key", ctypes.c_uint32 * (4 * 15)), ("rounds", ctypes.c_uint)]

    def _ige(data: bytes, key: bytes, iv: bytes, encrypt: bool) -> bytes:
        aes_key = AES_KEY()
        k = (ctypes.c_ubyte * len(key)).from_buffer_copy(key)
        v = (ctypes.c_ubyte * len(iv)).from_buffer_copy(iv)
        src = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        out = (ctypes.c_ubyte * len(data))()
        setter = lib.AES_set_encrypt_key if encrypt else lib.AES_set_decrypt_key
        setter(k, ctypes.c_int(8 * len(key)), ctypes.byref(aes_key))
        lib.AES_ige_encrypt(src, out, ctypes.c_size_t(len(data)), ctypes.byref(aes_key), v, ctypes.c_int(int(encrypt)))
        return bytes(out)

    libssl.decrypt_ige = lambda data, key, iv: _ige(data, key, iv, False)
    libssl.encrypt_ige = lambda data, key, iv: _ige(data, key, iv, True)
    return "openssl"
