# LensPilot

摄影师一站式工作流桌面软件：拷卡、AI 选片、照片编辑与预设、去重、水印、云端交付、AI 助手 Agent。

## 功能模块

| 模块 | 说明 |
|---|---|
| 💾 **自动拷卡** | 检测 SD 卡 / 移动存储 → 去重拷贝 → MD5 校验 → 多卡排队；FTP 相机传输；文件夹监听（联机拍摄） |
| 🤖 **AI 选片** | ResNet-18 / 多任务 UnifiedPhotoQualityNet 美学评分 + 模糊检测 + 眼部状态 + 去重特征；Score-CAM / Grad-CAM 热力图；ONNX CPU 或 PyTorch GPU 后端可切换 |
| 🎨 **照片编辑与预设** | 非破坏调色/裁剪/蒙版/污点/版本；预设卡片墙（悬停预览、0–200% 强度、选区批量、自适应 look transfer、从成片学预设、XMP 导入导出）；照片编辑页网格直接套用预设 |
| ☁ **云端管理**（可选） | 登录云账号后：上传备份、在线浏览、客户相册交付。不登录完全不影响本地功能 |
| ⚡ **自动化** | 全自动 拷卡→选片→上传 流水线 + 定时执行 |
| ✨ **AI 助手** | 大模型 Agent（OpenAI 兼容函数调用），100+ 个工具操作照片、写报告、检索知识库、定时任务 + 热文件夹监听触发 |

## 环境要求

### 开发环境
- Node.js >= 18
- Python >= 3.10
- ONNX 版（CPU，轻量）：`onnxruntime`
- 或 PyTorch 版（GPU，高性能）：`torch >= 2.0 + torchvision`
- 模型文件通过首次启动自动下载，无需手动放置

### 安装

```bash
cd electron_app
npm install

# 安装 Python 依赖（二选一）：
# CPU / ONNX 版（~60MB）：
pip install -r python_backend/requirements_onnx.txt

# GPU / PyTorch 版（~930MB）：
pip install -r python_backend/requirements_cuda.txt
```

### 运行

```bash
npm start
```

### 打包

```bash
# Windows CPU 版（Inno Setup 安装程序）
.\build-win-cpu.bat

# Windows GPU 版
.\build-win-gpu.bat

# macOS CPU 版
./build-mac.sh cpu

# macOS GPU 版 (MPS)
./build-mac.sh gpu

# Linux CPU / GPU
./build-cpu.sh
./build-gpu.sh
```

## 项目结构

```
electron_app/
├── main.js                 # Electron 主进程 (IPC, 构建ID, 导出/导入)
├── preload.js              # 安全桥接 API
├── python_service.js       # Python 子进程管理 (PyInstaller/service.exe)
├── electron-builder.yml    # 打包配置
├── package.json
├── clean-before-build.py   # 打包前清理开发机残留 + 生成 build-id
├── build-win-cpu.bat        # 打包脚本 (Win CPU)
├── build-win-gpu.bat        # 打包脚本 (Win GPU)
├── build-mac.sh             # 打包脚本 (macOS)
├── build-cpu.sh             # 打包脚本 (Linux CPU)
├── build-gpu.sh             # 打包脚本 (Linux GPU)
├── src/                    # 前端 UI
│   ├── index.html
│   ├── css/style.css
│   └── js/
│       ├── app.js          # 主控制器
│       ├── agent.js        # AI 助手 (对话/设置/知识库/提示词/定时任务/历史)
│       ├── components.js   # UI 组件工具
│       ├── copy-card.js    # 拷卡 / FTP / 文件夹监听页面
│       ├── photo-select.js # 选片页面 (评分/网格/灯箱/热力图)
│       ├── file-browser.js  # 文件浏览器
│       ├── uploader.js     # 云端上传
│       ├── automation.js   # 自动化流水线 (定时 拷卡→选片→上传)
│       └── i18n.js
├── python_backend/         # Python 后端
│   ├── service.py          # IPC 消息循环 + 配置管理 + 崩溃日志 + subscribe_events
│   ├── copy_card.py        # SD 卡检测 / 拷贝 / MD5 校验
│   ├── photo_scorer.py     # AI 评分 / 筛选 / 文件操作 / 水印 / 模糊/眼部/去重
│   ├── model_loader.py     # ONNX / PyTorch 模型加载 + CAM 热力图
│   ├── score_cache.py      # 评分缓存 (SQLite)
│   ├── cloud_uploader.py   # OSS 上传 (多段+断点续传)
│   ├── ftp_server.py       # FTP 服务器 (相机无线传输)
│   ├── folder_watcher.py   # 文件夹监听 (联机拍摄 + 稳定性判断)
│   ├── agent_module.py     # ★ AI 助手 Agent 核心 (34 工具 + 知识库)
│   ├── agent_watcher.py    # ★ 热文件夹监听触发 Agent
│   ├── requirements_onnx.txt
│   ├── requirements_cuda.txt
│   └── crash_logs/
├── build-resources/        # 打包中间产物 (由 build 脚本生成)
└── model/                  # AI 模型文件 (不打包，首启自动下载)
```

## 技术架构

- **前端**: Electron + 原生 HTML/CSS/JS (无框架)
- **后端**: Python (child_process → stdin/stdout JSON-line 协议)
- **通信**: JSON 协议，Electron renderer → IPC → main → Python subprocess (daemon handler threads)
- **AI 模型**: ResNet-18 / UnifiedPhotoQualityNet (ONNX CPU + PyTorch GPU 双后端, 首次启动从服务器下载)
- **评分**: 美学评分 + 多任务 (模糊/眼部/去重特征)
- **打包**: electron-builder (Electron) + PyInstaller --onedir (Python)

## AI 助手 Agent 模块

### 概述
Agent 在 Python 后端运行（`agent_module.py`），将 OpenAI 兼容的函数调用循环与 `photo_scorer` 的处理程序进行进程内集成。LLM 通过聊天界面控制、定时任务或在选片/文件页面上右键单击来调用。

### 模型配置
- 任何 OpenAI 兼容端点 (GLM / Qwen / DeepSeek / Claude API / 自定义)
- 思考深度 (reasoning_effort): off / low / medium / high / max
- 配置保存在 `app_config.json` 中；新构建不会保留

### 安全模型
- **沙盒**: 每个文件路径都经过 `os.path.realpath` 校验，以防止越权访问。Agent 只能操作选定工作文件夹内的文件
- **确认门**: 破坏性工具 (delete / rename / dedup_burst / move_photo / set_work_folder) 需要用户确认，除非 `auto_approve` 已开启
- **仅在 Python 封装层执行**——LLM 从未拥有路径权限

### 工具 (共 34 个)

**只读 — 浏览 / 质量检查:**
`list_photos`, `scan_count`, `get_model_status`, `get_scores`, `get_photo_info`, `get_heatmap`, `detect_blur`, `detect_eyes`, `deep_dedup`, `get_exif`, `compare_photos`, `search_knowledge_base`

**评分 — 导出:**
`score_photos`, `select_above_threshold`, `apply_selection`, `apply_watermark`, `organize_by_score`, `upload_to_cloud`, `export_image`

**文件操作:**
`copy_photo`, `move_photo`, `create_folder`, `rename_photo`, `delete_photo`, `dedup_burst`, `set_keep`, `set_reject`, `set_work_folder`

**文档 (修图报告等):**
`write_document`, `read_document`, `list_documents`, `append_document` (支持 .md / .txt / .docx)

**交互:**
`ask_user` (向用户提问澄清需求), `finish`

### 对话历史
- 每次交互会自动保存在 `localStorage` 中
- 顶部工具栏中的导出 (.json) / 导入 / 清空按钮
- 关闭应用时自动保存；启动时恢复

### 定时任务 + 热文件夹监听
- **定时任务**: 每 N 分钟 / 每小时 / 每天 / 每周 / 一天多次
- **监听任务**: 监听文件夹，当新文件数达到阈值 N 且静默期过后，自动触发 agent
- 每任务覆盖: 模型选择 / 思考深度 / 仅新增文件 / 自动批准
- 运行历史 (最近 10 次) 显示在任务卡片上；失败自动重试一次
- 任务保存在 `localStorage` 中；新构建不会保留

### 知识库 + 提示词库
- **知识库**: `APS_USER_DATA/agent_kb/` 下的 Markdown 文件；`search_knowledge_base` 工具通过关键词重叠进行搜索
- **提示词库**: 保存在配置中的命名提示词模板；输入框旁的下拉菜单可一键填入

### Token 使用情况
- 输入 / 输出 / 缓存 / 总计的累计统计，附带 SVG 历史图表
- 使用数据保存在 `agent_usage.json` 中；新构建不会保留

## 许可

- **代码**：GPL-3.0-or-later（完整文本见仓库根目录 [LICENSE](../LICENSE)）
- **AI 模型权重**：CC-BY-NC 4.0 双许可 — 非商业用途免费（首次使用自动下载）；商业用途（职业交付、企业集成、再分发等）需另行取得授权，见
  [python_backend/photo_s_plugin_auto_tone/LICENSE-WEIGHTS.txt](python_backend/photo_s_plugin_auto_tone/LICENSE-WEIGHTS.txt)
- 第三方组件许可：[THIRD_PARTY_LICENSES.txt](THIRD_PARTY_LICENSES.txt)

## 作者

© 2026 Dongwu259 (Dongwu259)
