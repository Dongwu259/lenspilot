# LensPilot

开源免费的摄影师一站式工作流桌面软件：拷卡 → AI 选片 → 照片编辑与预设 → 批量导出 → 云端交付（可选）。

> 代码以 **GPL-3.0-or-later** 开源；AI 模型权重以 **CC-BY-NC 4.0** 向非商业用途开放（商业授权另行提供）。

## 功能

| 模块 | 说明 |
|---|---|
| 💾 **自动拷卡** | 检测 SD 卡 / 移动存储 → 去重拷贝 → MD5 校验 → 多卡排队；FTP 相机传输；文件夹监听（联机拍摄） |
| 🤖 **AI 选片** | 美学评分 + 模糊检测 + 眼部状态 + 去重特征；Score-CAM / Grad-CAM 热力图；ONNX CPU / PyTorch GPU 双后端 |
| 🎨 **照片编辑与预设** | 非破坏调色/裁剪/蒙版/污点/版本；预设卡片墙、批量套用、自适应 look transfer、XMP 导入导出 |
| 🖨 **批量导出** | 多格式批量导出，可选文字/图片水印 |
| ⚡ **自动化** | 拷卡→选片→导出/上传 流水线 + 定时任务 |
| ✨ **AI 助手** | 本地/自备大模型 Agent，100+ 工具操作照片、写报告、知识库检索、定时与热文件夹触发 |
| ☁ **云端交付**（可选） | 登录云账号后：备份、在线浏览、客户相册交付。不登录不影响任何本地功能 |

所有功能**免费且无使用限制**：登录仅用于可选的云端服务。

## 仓库结构

```
├── electron_app/      # Electron 桌面应用（主程序）
│   ├── main.js / preload.js / src/     # Electron + 原生 JS 前端
│   ├── python_backend/                 # Python 后端（IPC 子进程，AI 评分/修图/Agent）
│   └── build-*.sh / build-win-*.bat    # Win / macOS / Linux 构建脚本
├── scripts/           # gphoto2（联机拍摄）构建脚本、LUT 生成、i18n 工具
├── smoke_test/        # 冒烟 / 回归测试（脱离 Electron 直连 Python 后端）
└── docs/              # 架构文档（命令注册表、硬件指南）
```

## 快速开始

### 开发运行

```bash
cd electron_app
npm install

# Python 依赖（二选一）：
pip install -r python_backend/requirements_onnx.txt   # CPU/ONNX 版（~60MB）
pip install -r python_backend/requirements_cuda.txt   # GPU/PyTorch 版（~930MB）

npm start
```

AI 模型权重在首次使用对应功能时自动下载，无需手动放置。

### 打包

```bash
cd electron_app
./build-win-cpu.bat      # Windows CPU 版安装程序
./build-win-gpu.bat      # Windows GPU 版安装程序
./build-mac.sh cpu       # macOS CPU 版（gpu 为 MPS 版）
./build-cpu.sh           # Linux CPU 版
```

## 模型权重许可（重要）

本软件运行所需的 AI 模型权重文件**不适用 GPL-3.0**，采用 CC-BY-NC 4.0 双许可：

- **非商业用途**（个人学习/研究/个人照片处理）：免费，自动下载即可使用
- **商业用途**（职业交付、企业内部使用、产品集成、再分发）：需联系作者取得授权

权重许可全文：[electron_app/python_backend/photo_s_plugin_auto_tone/LICENSE-WEIGHTS.txt](electron_app/python_backend/photo_s_plugin_auto_tone/LICENSE-WEIGHTS.txt)

## 文档

- [应用架构与构建说明](electron_app/README.md)
- [命令注册表（IPC/UI/Agent 共享命令）](docs/COMMAND_REGISTRY.md)
- [硬件兼容指南](docs/hardware-guide.zh.md)
- [联机拍摄（gphoto2）Windows 构建说明](scripts/gphoto2_windows_setup.md)

## 测试

```bash
python smoke_test/smoke_test.py                 # 基础冒烟测试（无需 API key）
node electron_app/../smoke_test/_check_i18n_keys.js   # 从 electron_app/ 目录运行
```

## 协议

- 代码：[GPL-3.0-or-later](LICENSE)
- 模型权重：CC-BY-NC 4.0（见上文）
- 第三方组件：[electron_app/THIRD_PARTY_LICENSES.txt](electron_app/THIRD_PARTY_LICENSES.txt)

## 作者

© 2026 Dongwu259 (Dongwu259) — [dwphoto.top](https://dwphoto.top)
