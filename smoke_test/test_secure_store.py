# -*- coding: utf-8 -*-
"""secure_store 冒烟：DPAPI 加解密往返 / 幂等 / 旧明文兼容。

用法: python smoke_test/test_secure_store.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "electron_app" / "python_backend"))

import secure_store  # noqa: E402

PLAIN = "sk-test-12345-大写ABC+/="


def main():
    stored = secure_store.protect(PLAIN)
    assert stored != PLAIN, "protect 后不应仍是明文"
    if sys.platform == "win32":
        assert secure_store.is_protected(stored) and stored.startswith("dpapi:"), stored[:16]
    back = secure_store.unprotect(stored)
    assert back == PLAIN, "DPAPI 往返失败"
    # 幂等：已保护值不再二次加密
    assert secure_store.protect(stored) == stored
    # 旧明文（无前缀）透传兼容——升级前写入的配置继续可读
    assert secure_store.unprotect(PLAIN) == PLAIN
    # plain: 前缀（非 Windows 路径）
    assert secure_store.unprotect("plain:" + PLAIN) == PLAIN
    # 空值/坏密文不抛错
    assert secure_store.protect("") == ""
    assert secure_store.unprotect("") == ""
    assert secure_store.unprotect("dpapi:not-base64!!!") == ""
    print(f"[ok] secure_store: 往环/幂等/旧明文兼容/坏值静默 "
          f"(platform={sys.platform}, form={stored.split(':', 1)[0]})")


if __name__ == "__main__":
    main()
