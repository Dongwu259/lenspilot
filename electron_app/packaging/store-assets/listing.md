# Microsoft Store 上架材料 — LensPilot v0.9.0

> Partner Center 提交流程见文末「操作清单」。素材：`store-logo-300x300.png`、
> `hero-1920x1080-zh/en.png`、`../msix/Assets/`（包内磁贴图标）。截图见「截图清单」。

---

## zh-CN（简体中文，主语言）

**产品名称（保留名建议）**：LensPilot

**商店显示名**：LensPilot - AI 摄影师工作流

**简短说明（搜索结果摘要）**：
AI 摄影师工作流软件：智能选片、一键修图、批量调色、客户交付。开源免费，AI 全部本地运行，照片不出你的电脑。

**完整描述**：

LensPilot 把摄影师从拷卡到交付的完整流程装进一个桌面应用：导入 → AI 选片 → AI 修图 → 精修 → 客户交付。软件开源免费（GPL-3.0），AI 推理全部在你的电脑上完成，照片像素不经手任何第三方服务器。

【AI 选片】
· 理解整组照片的批次风格与光线，同一批照片统一校准评分
· 自动分层：精华 / 可用 / 待淘汰，支持详情对比与快速挑选
· 人脸检测辅助，闭眼、模糊张自动降权

【AI 修图】
· 自研修图模型一键出图：曝光、白平衡、色调、分级等参数自动给出
· 批量应用：整组照片一次统一风格，再逐张微调
· 预设卡片墙：悬停即时预览，从你自己的成片中学习风格
· 支持 XMP 预设导入导出，与 Lightroom 生态互通

【专业编辑器】
· RAW 格式支持（Canon / Nikon / Sony / Fuji 等）
· 分阶段修图：构图 → 曝光 → 色彩 → 风格，逐步推进
· 构图候选与自动找平、Compare / Survey 对比模式、缩放到 1600% 检查细节
· 批量导出可中断、断点续传

【数字员工 / 自动化】
· 内置本地大模型驱动的 AI 助手，100+ 工具可直接操作软件
· 定时任务、监控文件夹：拷卡目录一有新照片自动进入流水线

【联机拍摄】
· 支持 Canon / Nikon / Sony 等相机联机（Tethered）拍摄，即拍即看

【客户交付】
· 云端相册：客户打开链接即可浏览、批量下载原片或成片
· 免费云空间 2GB、10 个相册；更多空间可订阅云端版（可选，软件功能不受影响）

【隐私与开源】
· AI 推理 100% 本地（ONNX / llama.cpp），离线可用
· 代码以 GPL-3.0 开源：https://github.com/Dongwu259/lenspilot
· 模型权重以 CC BY-NC 4.0 授权，商用需另行授权
· 首次使用会自动下载 AI 模型权重（约 50MB，断点续传 + SHA256 校验）

【说明】
· 本商店版本为 CPU 推理版，适用于所有 Windows 10/11 x64 电脑；NVIDIA GPU 加速版请到 GitHub Releases 下载
· 联机拍摄首次使用时需按软件内指引安装相机驱动（Windows 会弹出管理员确认）
· 云端功能需登录免费账号，可完全跳过

**更新说明（v0.9.0 首发版本）**：
首个 Microsoft Store 版本：AI 选片、AI 修图、专业编辑器、数字员工、客户交付全功能开放。开源版移除了所有功能限制，云端存储为可选订阅。

**关键词（7 个，每个 ≤30 字符）**：
摄影修图 / AI选片 / RAW编辑 / 照片调色 / 摄影师工具 / 批量修图 / Lightroom

**类别**：照片和视频（Photo & video）
**子类别**：照片编辑

**年龄分级**：问卷全部选"否"，预期分级 3+（Everyone）

---

## en-US（英语）

**Store display name**: LensPilot - AI Photo Workflow

**Short description**:
AI workflow for photographers: smart culling, one-click retouching, batch color grading, and client delivery. Free & open source. All AI runs locally — your photos never leave your PC.

**Description**:

LensPilot puts the whole photographer workflow — import, AI culling, AI retouching, fine editing, client delivery — into one desktop app. It is free and open source (GPL-3.0), and every AI inference runs on your own machine: photo pixels are never uploaded to any third-party server.

[AI Culling]
· Understands batch style and lighting across the whole shoot, calibrating scores consistently
· Auto tiers: keep / maybe / reject, with detail compare and rapid selection
· Face detection helps down-weight closed eyes and blur

[AI Retouching]
· A custom retouching model suggests exposure, white balance, tone and grading in one click
· Batch apply: unify a whole shoot, then fine-tune per photo
· Preset card wall with hover preview; learn a look from your own finished work
· XMP preset import/export, interoperable with the Lightroom ecosystem

[Pro Editor]
· RAW support (Canon / Nikon / Sony / Fuji and more)
· Stage-wise editing: crop → exposure → color → style
· Crop candidates with auto-level, Compare / Survey modes, up to 1600% zoom
· Interruptible batch export with resume

[Digital Employee / Automation]
· A local-LLM assistant with 100+ tools that operate the app for you
· Scheduled tasks and watch folders: new files enter the pipeline automatically

[Tethered Shooting]
· Canon / Nikon / Sony tethered capture with instant preview

[Client Delivery]
· Cloud albums: clients open a link to browse and bulk-download
· Free 2GB cloud storage and 10 albums; more via optional subscription (app features are never locked)

[Privacy & Open Source]
· 100% local AI (ONNX / llama.cpp), works offline
· GPL-3.0 source: https://github.com/Dongwu259/lenspilot
· Model weights under CC BY-NC 4.0; commercial use requires a license
· First run downloads model weights (~50MB, resumable, SHA256-verified)

[Notes]
· This store build is the CPU edition for all Windows 10/11 x64 PCs; the NVIDIA GPU build is on GitHub Releases
· Tethered shooting needs a camera driver install on first use (Windows UAC prompt)
· Cloud features need a free account and can be skipped entirely

**What's new (v0.9.0)**:
First Microsoft Store release: AI culling, AI retouching, pro editor, digital employee and client delivery, all unlocked. The open-source edition has no feature gates; cloud storage is an optional subscription.

**Keywords**: photography / AI culling / RAW editor / photo grading / retouching / batch edit / tethering

**Category**: Photo & video

---

## Partner Center 操作清单

1. **注册开发者账号**：https://developer.microsoft.com/microsoft-store → 注册（个人账号一次性 $19；公司账号 $99）。账号实名完成后才能提交。
2. **保留应用名称**：Partner Center → 应用和服务 → 新建应用 → 保留名称 `LensPilot`（显示名可用 "LensPilot - AI 摄影师工作流"）。
3. **取回应用标识**：应用标识页复制 **Package/Identity/Name** 与 **Publisher**（形如 `CN=XXXX…`），填入 `electron_app/packaging/msix/appx-identity.json`。
4. **重新打包**：`cd electron_app && build-win-store.bat`（未签名包即为商店上传包；不要对商店包自签名）。
5. **提交包**：提交 → 软件包 → 上传 `package\LensPilot_0.9.0.0_x64.msix`。
6. **商店一览（Store listing）**：按上文 zh-CN / en-US 填写；上传 store-logo-300x300.png、hero-1920x1080、截图（≥1 张）。
7. **属性**：类别=照片和视频；声明：应用含第三方开源代码（GPL-3.0，源码链接 GitHub）；可访问性声明按默认；系统要求勾选 Windows 10+ 桌面版。
8. **年龄分级问卷**：全部"否"（无暴力/赌博/成人内容/在线交互），预期 3+。
9. **认证备注（Notes for certification）**（英文填写，供审核员参考）：
   > LensPilot is a free open-source (GPL-3.0) desktop app for photographers. On first use it downloads its AI model weights (~50MB) from the developer's own server over HTTPS and then runs fully offline. Tethered shooting (optional, in-app guided) triggers a standard Windows UAC prompt to install a camera driver. Cloud album features require a free account on the developer's service and are optional — all app features work without an account. No paid content is gated inside the app.
10. **提交认证**：一般 24–48 小时出结果；被拒时按报告修复后重新提交同一包（包号会自动 +1）。

## 截图清单（Partner Center 至少 1 张，推荐 4–8 张）

- 规格：PNG/JPG，≥1366x768，推荐 1920x1080 或 2560x1440，单张 ≤50MB（桌面类）
- 必拍：
  1. 工作台总览（胶片带 + 网格 + 右侧信息栏）
  2. AI 选片评分页（评分分层 + 精华标记）
  3. AI 修图前后对比（auto-tone 一键出图）
  4. 编辑器（预设卡墙悬停预览 或 分阶段修图卡）
  5. 客户交付相册（网页视角截图亦可，标注"客户视角"）
- 建议带真实照片素材（婚礼/人像/风光任一组 30+ 张），空状态界面说服力弱

## 常见审核风险预案

- **"应用下载可执行内容"**：模型权重是数据文件（.onnx/.gguf）而非可执行代码，Auto-tone 运行时随包内置；如被问，回复认证备注第 1 条。
- **"UAC 提权"**：联机拍摄驱动安装是可选功能，走标准 UAC 弹窗，属桌面桥（Full Trust）允许范围。
- **"账号要求"**：登录完全可选，审核员不登录即可用全部本地功能。
- **GPL 分发**：微软商店开发者协议 4(c) 明确 GPL 应用可上架（商店条款不对 GPL 软件额外设限）；源码链接已在一览与描述中给出。
