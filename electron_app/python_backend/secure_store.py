# -*- coding: utf-8 -*-
"""敏感配置的静态加密（借鉴 lumen-raw 的 DPAPI 方案）。

protect(value)   → 存盘形态（带前缀）
unprotect(stored) → 明文（任何失败返回 ''，无前缀的旧明文原样透传兼容）

- Windows: DPAPI（crypt32 CryptProtectData/CryptUnprotectData），
  密文绑定当前用户账户，落盘形如 'dpapi:<base64>'；
- 非 Windows: 'plain:' 前缀透传（配置文件仍为明文，与旧行为一致；
  macOS 钥匙串接入见 TODO——当前 mac 构建未分发云端 Key 功能）。

设计约束：任何路径不抛错——加解密失败只影响该值可读性，不炸宿主流程。
"""
import base64
import os
import sys

_PREFIX_DPAPI = "dpapi:"
_PREFIX_PLAIN = "plain:"


def _dpapi_blob(data: bytes, protect: bool) -> bytes:
    """ctypes 调 crypt32。DATA_BLOB 内存由 LocalFree 释放。"""
    import ctypes
    from ctypes import wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    pin = _DATA_BLOB(len(data),
                     ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    pout = _DATA_BLOB()
    func = ctypes.windll.crypt32.CryptProtectData if protect \
        else ctypes.windll.crypt32.CryptUnprotectData
    if not func(ctypes.byref(pin), None, None, None, None, 0, ctypes.byref(pout)):
        raise OSError("crypt32 failed")
    try:
        return ctypes.string_at(pout.pbData, pout.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(pout.pbData)


def protect(value: str) -> str:
    """明文 → 存盘形态。已保护的值原样返回（幂等）。"""
    value = value or ""
    if not value or value.startswith((_PREFIX_DPAPI, _PREFIX_PLAIN)):
        return value
    if os.name == "nt":
        try:
            blob = _dpapi_blob(value.encode("utf-8"), protect=True)
            return _PREFIX_DPAPI + base64.b64encode(blob).decode("ascii")
        except Exception:
            pass  # DPAPI 不可用（极端环境）→ 退 plain，不炸调用方
    return _PREFIX_PLAIN + value


def unprotect(stored: str) -> str:
    """存盘形态 → 明文。旧明文（无前缀）透传；解密失败返回 ''。"""
    if not stored:
        return ""
    if stored.startswith(_PREFIX_PLAIN):
        return stored[len(_PREFIX_PLAIN):]
    if not stored.startswith(_PREFIX_DPAPI):
        return stored  # 历史明文（升级前写入），继续可读
    try:
        blob = base64.b64decode(stored[len(_PREFIX_DPAPI):])
        return _dpapi_blob(blob, protect=False).decode("utf-8")
    except Exception:
        return ""


def is_protected(stored: str) -> bool:
    return bool(stored) and stored.startswith(_PREFIX_DPAPI)
