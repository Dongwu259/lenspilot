#!/usr/bin/env python3
"""
clean-before-build.py — 打包前清理开发机残留，确保分发包干净（P0: 登录态/配置不泄漏）

在打包前运行，清理：
  1. python_backend/app_config.json  → 重置为干净默认（去掉开发机的 copy_target_path 等）
  2. python_backend/crash_logs/      → 清空崩溃日志
  3. python_backend/__pycache__/     → 清空 Python 缓存
  4. build-resources/tmp/            → 清空 PyInstaller 临时构建文件
  5. build-resources/python_backend/ → 清空旧的打包产物（service.exe / service），打包时会重新生成
  6. model/ 下的 .tmp                → 清空下载中断的临时文件

用法（在 electron_app/ 目录）：
    python clean-before-build.py

注意：不清理 build-resources/model/（模型文件，按需保留/删除由打包文档第七步决定）。
      localStorage（登录态）在运行时存于用户数据目录，不进包；本脚本主要清开发机
      写进源码目录的配置/日志/缓存，防止它们被 extraResources 打进包。
"""
import os
import shutil
import json
import secrets

HERE = os.path.dirname(os.path.abspath(__file__))


def clean_dir(rel):
    p = os.path.join(HERE, rel)
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)
        print(f"  cleared: {rel}")
    os.makedirs(p, exist_ok=True)


def clean_file(rel):
    p = os.path.join(HERE, rel)
    if os.path.exists(p):
        os.remove(p)
        print(f"  removed: {rel}")


def main():
    print("[clean-before-build] 清理开发机残留...")

    # 0. Generate a fresh buildId → build-resources/build-id.json.
    # main.js purges login state on first launch of each buildId, so a
    # distributed package never ships with the developer's credentials.
    build_id = secrets.token_hex(8)
    br = os.path.join(HERE, 'build-resources')
    os.makedirs(br, exist_ok=True)
    with open(os.path.join(br, 'build-id.json'), 'w', encoding='utf-8') as f:
        json.dump({"buildId": build_id}, f)
    print(f"  wrote:  build-resources/build-id.json (buildId={build_id})")

    # 1. app_config.json → 重置为干净默认
    cfg_path = os.path.join(HERE, 'python_backend', 'app_config.json')
    clean_cfg = {"config": {}, "presets": {
        "索尼微单": {
            "photo_scan_path": "DCIM",
            "video_scan_path": os.path.join("PRIVATE", "M4ROOT", "CLIP"),
        }
    }, "current_preset": "索尼微单"}
    with open(cfg_path, 'w', encoding='utf-8') as f:
        json.dump(clean_cfg, f, ensure_ascii=False, indent=2)
    print("  reset:  python_backend/app_config.json (clean defaults)")

    # 2. crash_logs / __pycache__
    clean_dir('python_backend/crash_logs')
    clean_dir('python_backend/__pycache__')

    # 3. build-resources/tmp (PyInstaller temp)
    clean_dir('build-resources/tmp')

    # 4. build-resources/python_backend (old packaged service exe/bin)
    clean_dir('build-resources/python_backend')

    # 5. model download .tmp leftovers
    model_dir = os.path.join(HERE, 'model')
    if os.path.isdir(model_dir):
        for f in os.listdir(model_dir):
            if f.endswith('.tmp'):
                try:
                    os.remove(os.path.join(model_dir, f))
                    print(f"  removed: model/{f}")
                except OSError:
                    pass

    print("[clean-before-build] 完成。现在可以打包了。")
    print("  提示: localStorage 登录态在用户数据目录（不进包），无需清理；")
    print("        若担心 Windows portable 同目录残留，分发前确认 exe 同目录无 LensPilot 子目录。")


if __name__ == '__main__':
    main()
