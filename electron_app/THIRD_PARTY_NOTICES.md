# 第三方组件声明 / Third-Party Notices

本软件包含以下第三方组件：

## OpenCLIP（AI 自动修图视觉塔推理）

- **文件**：vendored 于 `python_backend/photo_s_plugin_auto_tone/`（插件代码，MIT）；运行时按需下载的 CLIP/SigLIP 视觉塔权重经 modelstore 缓存
- **来源**：OpenCLIP（<https://github.com/mlfoundations/open_clip>）
- **许可证**：Apache License 2.0
- **用途**：AI 自动修图（auto-tone）的视觉特征提取。仅在本地运行，不上传任何图像。
- **模型权重许可**：auto-tone 预测器/视觉塔权重（modelstore 按需下载）为 CC-BY-NC 4.0，作者为本项目所有者。

---

## MediaPipe Face Landmarker

- **文件**：`model/face_landmarks.onnx`（由 Google 官方 `face_landmarker.task` 中的 `face_landmarks_detector.tflite` 转换为 ONNX）
- **来源**：Google MediaPipe（<https://ai.google.dev/edge/mediapipe>）
- **许可证**：Apache License 2.0
- **版权**：Copyright 2023 The MediaPipe Authors.
- **用途**：本地人脸关键点检测，用于几何法（Eye Aspect Ratio）判断眼睛开合。仅在本地运行，不上传任何图像。

> Licensed under the Apache License, Version 2.0 (the "License"); you may not use
> this file except in compliance with the License. You may obtain a copy of the
> License at http://www.apache.org/licenses/LICENSE-2.0
>
> Unless required by applicable law or agreed to in writing, software distributed
> under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
> CONDITIONS OF ANY KIND, either express or implied.

---

## libgphoto2

- **文件**：libgphoto2 camlibs（通过 python-gphoto2 动态链接加载）
- **来源**：libgphoto2 project（<https://github.com/gphoto/libgphoto2>）
- **许可证**：GNU Lesser General Public License v2.1 or later (LGPL v2.1+)
- **版权**：Copyright (c) 2000-2024, Lutz Mueller and contributors.
- **用途**：相机 USB 直连控制——实时取景、遥控拍摄、光圈/快门/ISO 参数读写。支持 ~2000+ 相机型号。
- **链接方式**：动态链接（加载系统的 .so/.dylib/.dll），未修改 libgphoto2 源代码，未静态链接进安装包。

> This library is free software; you can redistribute it and/or modify it under
> the terms of the GNU Lesser General Public License as published by the Free
> Software Foundation; either version 2.1 of the License, or (at your option)
> any later version. Full text at https://www.gnu.org/licenses/lgpl-2.1.html

---

## libwdi (Windows USB Driver Installation)

- **文件**：wdi-simple.exe（由 libwdi 源码编译的独立工具）
- **来源**：libwdi project（<https://github.com/pbatard/libwdi>）
- **许可证**：LGPL v2.1+（库部分）/ GPL v3（Zadig 应用部分）
- **版权**：Copyright (c) 2010-2024, Pete Batard and contributors.
- **用途**：为相机安装 WinUSB 设备驱动（替换 Windows 默认 PTP 类驱动），使 libgphoto2 能够通过 libusb 访问相机。仅在 Windows 平台使用，由用户主动触发。

> wdi-simple is distributed under the terms of the LGPL v2.1 or later.
> Source code at https://github.com/pbatard/libwdi

---

## libusb

- **文件**：libusb-1.0.dll（Windows）/ 系统库（macOS/Linux）
- **来源**：libusb project（<https://github.com/libusb/libusb>）
- **许可证**：GNU Lesser General Public License v2.1 or later (LGPL v2.1+)
- **版权**：Copyright (c) 2001-2024, libusb contributors.
- **用途**：跨平台 USB 设备访问，libgphoto2 和 libwdi 的底层传输层。
