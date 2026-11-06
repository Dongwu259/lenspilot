# LensPilot 硬件指南

> 更新日期:2026-09-05
> 适用版本:LensPilot 0.9.0
> 资源占用数据为 Windows 开发机实测(2026-09);Mac 的 Metal 数据见「Mac 配置」节

本文回答三个问题:
1. 软件里跑的两个模型各自要多少内存/显存?
2. 能不能完全靠 CPU 跑?
3. CPU(ONNX)/ GPU(torch) 两个版本分别适配什么机器?

---

## 一、软件跑的两个模型

| 模型 | 用途 | 推理引擎 | 模型文件 |
|---|---|---|---|
| **Qwen3-VL-2B-Instruct**(~2B 级多模态) | AI 助手 / 数字员工对话、决策、看图 | llama.cpp(`llama-server` 子进程) | `Qwen3VL-2B-Instruct-Q4_K_M.gguf` + `mmproj-Qwen3VL-2B-Instruct-F16.gguf` |
| **ResNet-18**(1170 万参数) | AI 选片评分(闭眼/模糊/质量) | ONNX Runtime 或 PyTorch | `best_model.onnx(+.data)` 或 `best_model.pth` + `resnet18.yaml` |

两个模型**独立运行、互不依赖**。本地大模型替代官方 API Key;AI 选片模型负责选片打分。

---

## 二、实测资源占用

### 本地大模型(Qwen3-VL 2B,llama.cpp)

| 项 | 实测值 |
|---|---|
| GGUF 权重(Q4_K_M) | **1107 MB** |
| 视觉编码器(mmproj f16) | **819 MB** |
| 磁盘合计 | **~1.93 GB**(ModelScope 国内直连下载,断点续传) |
| 参数量级 | ~2B |
| **纯 CPU 运行内存**(工作集) | **2 – 3 GB RAM**(2B Q4 量级) |
| CPU 推理速度 | 待实测(2B Q4 约每秒十几 token,对话可用) |
| **GPU 显存**(CUDA/Metal 全量 offload) | **~2 GB VRAM** |
| GPU 推理速度 | 实测 **~35 tok/s**(含首响,2026-09-05 开发机;MiniCPM 时代同机实测 174 tok/s 纯生成,供参考) |

**结论:可以完全由 CPU 运行**,GPU 只加速。

### AI 选片模型(ResNet-18)

| 项 | 实测/文档值 |
|---|---|
| ONNX 版(CPU)磁盘 | **~45 MB**(权重 30MB + 运行时) |
| PyTorch 版(GPU)磁盘 | **~130 MB 模型 + ~800 MB torch 运行时** |
| 纯 CPU 推理内存(峰值) | **0.5 – 1 GB RAM** |
| CPU 速度 | 单张 224×224 前向 ~10–30ms,一秒几十张 |
| GPU 显存(CUDA/MPS) | **0.5 – 1 GB VRAM**(可选项) |

**结论:可以完全由 CPU 运行**,且 ResNet-18 极小,选片瓶颈在磁盘读图而非算力。

---

## 三、两个版本与适配矩阵

| 版本 | 打包后端 | AI 选片 | 本地大模型 |
|---|---|---|---|
| **CPU 版**(ONNX) | ONNX Runtime + **CPU 版 llama-server** | ✅ 全 CPU,零 GPU 依赖 | ✅ 全 CPU 可跑 |
| **GPU 版**(PyTorch) | PyTorch + **CUDA 版 llama-server** | ✅ torch 走 CUDA,**无 GPU 自动退 CPU** | ⚠️ **必须 NVIDIA 显卡+驱动**,否则起不来 |

关键差异(来自构建脚本 `build-win-cpu.bat` / `build-win-gpu.bat`):
- CPU 版打 `copy-llm-runtime.py --cpu` → CPU 版 llama-server,任何 x86-64 机器都能用本地大模型
- GPU 版打 `--cuda` → **CUDA 版 llama-server**,没有 NVIDIA 独显时本地大模型不可用(AI 选片 torch 仍会退 CPU 勉强可跑)

---

## 四、硬件要求速查

### 最低配置(纯 CPU,办公/核显机即可)

| 项 | 要求 |
|---|---|
| CPU | x86-64 4 核以上(需 AVX2,2013 年后几乎所有 CPU 都有) |
| 内存 RAM | **8 GB(紧张)/ 16 GB(推荐)** |
| 显卡 | 无独显,核显即可 |
| 磁盘 | 软件 + 本地模型 ≈ **4.5 GB 可用**(建议 10 GB) |

> 8GB 内存下,本地 LLM(≤3GB)+ Electron + Python 后端 + 系统会吃紧,建议 16GB。

### 推荐配置(专业使用)

| 项 | 要求 |
|---|---|
| CPU | 6–8 核,多核跑选片更快 |
| 内存 RAM | **16 GB 及以上** |
| 磁盘 | SSD,10 GB 以上(RAW 工作目录另计) |

### GPU 加速配置(可选,仅 NVIDIA)

| 项 | 要求 |
|---|---|
| 显卡 | NVIDIA,显存 **6 GB 以上** |
| 驱动 | 最新 NVIDIA 驱动(含 CUDA 12.x) |
| 效果 | 本地 LLM CPU→GPU 提速(Qwen3-VL 2B GPU 实测 ~35 tok/s);大批量选片 torch 加速 |

> 无 NVIDIA 独显的用户**不要装 GPU 版**(本地大模型会不可用),直接选 CPU 版。

### Mac 配置

| 项 | 要求 |
|---|---|
| 芯片 | **Apple Silicon(M1 及以上)** 推荐;Intel Mac 仅 CPU(可跑但慢) |
| 内存 | 8 GB 可跑 / 16 GB 推荐 |
| 推理 | llama.cpp **Metal** 版(Metal 不需要 NVIDIA,Mac 上 CPU/GPU 两版都兼容) |
| 构建 | `build-mac.sh cpu`(轻量,推荐)或 `gpu`(MPS) |

---

## 五、选版指南

| 你的机器 | 装哪个版本 |
|---|---|
| 无独显 / AMD / Intel 核显 | ✅ **CPU 版**(ONNX)—— 唯一能让本地大模型工作的选择 |
| 有 NVIDIA 显卡 | **GPU 版**(torch)—— 选片 + 本地 LLM 都加速 |
| Mac Apple Silicon | cpu 或 gpu 版均可(都走 Metal);轻量场景选 cpu 版 |
| 8GB 内存小机 | CPU 版;本地 LLM 可用但建议同时少开大程序 |

**通用建议**:AI 选片是 ResNet-18,CPU 就够快;本地大模型 2B Q4 纯 CPU 可跑,装 NVIDIA 显卡后明显提速。**绝大多数用户装 CPU 版即可满足全部功能**,GPU 版的主要价值 = 有 NVIDIA 显卡时的提速。

---

## 六、常见问题

**Q1:GPU 版装在无 NVIDIA 显卡的电脑上,本地大模型为什么用不了?**
GPU 版内置的是 CUDA 版 `llama-server`,必须 NVIDIA 显卡+驱动才能启动。无独显机器请装 CPU 版。

**Q2:8GB 内存能跑吗?**
能。本地大模型工作集 ≤3GB,选片 ≤1GB。但 8GB 下整机偏紧,建议 16GB。

**Q3:本地大模型慢怎么办?**
2B Q4 对话/工具调用已够流畅(GPU 实测 ~35 tok/s)。要更快:① 换 GPU 版(需 NVIDIA);② 减少上下文长度(设置里调低);③ 保持模型常驻(首次启动约 10-20 秒)。

**Q4:磁盘要留多少?**
本地模型 ~1.9GB + AI 选片模型 45MB(CPU)/ 130MB+torch 运行时(GPU)+ 软件本体。留 **10GB** 宽松。

**Q5:能只关掉本地大模型、只用 AI 选片吗?**
能。AI 助手设置里 `use_local` 关闭后,只跑选片,内存占用回到 ≤1GB(需个人云端 Key 才有对话能力)。

---

## 附:相关文档

- 本地 LLM 架构:`electron_app/python_backend/local_llm.py`
- AI 选片后端:`electron_app/python_backend/model_loader.py`(ONNX/PyTorch 后端切换)
- Mac 打包:参见 `electron_app/build-mac.sh` 与 `electron_app/build-local-mac.sh`
