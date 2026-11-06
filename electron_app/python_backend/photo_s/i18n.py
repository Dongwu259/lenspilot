"""PhotoS — Internationalization (CLI strings + language detection).

Shared module used by the CLI (``photo_s.cli``) and the GUI (``photo_s.gui``).

- ``detect_system_language()`` — cross-platform system-language probe
  (macOS / Windows / Linux), memoized, never raises.
- ``resolve_language()`` — precedence chain:
  explicit flag > ``PHOTO_S_LANG`` env > config ``language`` key >
  persisted GUI choice > system detection > ``"en"``.
- ``STRINGS`` — the CLI string table (zh/en key-parity enforced by tests),
  accessed via ``_t(key, lang, **kwargs)``. The GUI keeps its own ``STRINGS``
  in ``gui.py``; this module only shares detection / resolution / persistence.

Module-level imports are stdlib-only (``config`` is imported lazily) so that
neither the CLI nor the GUI forms an import cycle with the engine.
"""

import locale
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

SUPPORTED_LANGS = ("en", "zh")
DEFAULT_LANG = "en"          # fallback for missing keys / final fallback
CURRENT_LANG = DEFAULT_LANG  # set once by resolve_language() at startup
_detect_cache: Optional[str] = None  # memo: macOS subprocess runs once/process

# ── System language detection ──────────────────────────────────────────────

# LCIDs whose UI language is Simplified/Traditional Chinese.
_ZH_LCIDS = (0x0804, 0x0404, 0x0C04, 0x1004, 0x1404)  # CN TW HK SG MO


def _from_locale_string(value: str) -> Optional[str]:
    """Map a locale/env string like 'zh_CN.UTF-8' or 'en_US' to a supported lang."""
    v = value.strip().lower().replace("_", "-").split(".")[0]
    if v.startswith("zh"):
        return "zh"
    if v.startswith("en"):
        return "en"
    return None


def _env_language() -> Optional[str]:
    """POSIX locale env vars (LC_ALL > LC_MESSAGES > LANG)."""
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        val = os.environ.get(var)
        if val:
            lang = _from_locale_string(val)
            if lang:
                return lang
    return None


def _macos_apple_languages() -> Optional[str]:
    """macOS GUI apps (Finder launch) have no LANG — read the system list."""
    try:
        proc = subprocess.run(
            ["defaults", "read", "-g", "AppleLanguages"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    out = proc.stdout or ""
    # The plist is preference-ORDERED (e.g. ["en", "zh-Hans-CN"] means the
    # user prefers English). Take the FIRST supported entry — the old
    # "zh anywhere in the list" check misdetected English-first users.
    for token in re.findall(r'"([^"]+)"', out):
        lang = _from_locale_string(token)
        if lang:
            return lang
    # tolerate unquoted bare values in odd formats
    for token in out.replace(",", " ").replace("(", " ").replace(")", " ").split():
        lang = _from_locale_string(token.strip('"'))
        if lang:
            return lang
    return None


def _windows_ui_language() -> Optional[str]:
    """Windows: GetUserDefaultUILanguage() → LCID (0x0804 = zh-CN)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
    except Exception:
        return None
    return "zh" if lcid in _ZH_LCIDS else None


def _locale_module_language() -> Optional[str]:
    """locale module fallback (odd setups, non-macOS/Windows).

    Reads ``locale.getlocale()`` WITHOUT calling ``locale.setlocale`` — that
    call is process-global and permanently repins the C locale (breaking
    later ``open()`` default encodings) when env vars are stripped. Python
    already derives the initial locale from the environment at startup.
    """
    try:
        code = locale.getlocale()[0]
    except Exception:
        return None
    if not code:
        return None
    return _from_locale_string(code)


def detect_system_language() -> str:
    """Probe the system UI language across macOS / Windows / Linux.

    Each stage is isolated — any failure falls through to the next, and the
    whole call is wrapped so detection never crashes the CLI or GUI.
    """
    try:
        lang = _env_language()
        if lang:
            return lang
        if sys.platform == "darwin":
            lang = _macos_apple_languages()
            if lang:
                return lang
        elif sys.platform == "win32":
            lang = _windows_ui_language()
            if lang:
                return lang
        lang = _locale_module_language()
        if lang:
            return lang
    except Exception:
        pass
    return DEFAULT_LANG


def _system_language() -> str:
    """Memoized accessor — the macOS subprocess runs at most once per process."""
    global _detect_cache
    if _detect_cache is None:
        _detect_cache = detect_system_language()
    return _detect_cache


# ── Precedence chain ───────────────────────────────────────────────────────

def resolve_language(explicit: Optional[str] = None, *,
                     config_path: Optional[str] = None,
                     use_config: bool = True,
                     use_persisted: bool = False) -> str:
    """Resolve the effective language.

    Priority: explicit flag > ``PHOTO_S_LANG`` env > config ``language`` >
    persisted GUI choice > system detection > ``DEFAULT_LANG``.
    ``"auto"`` / ``None`` / invalid values fall through to the next layer.
    """
    if explicit in SUPPORTED_LANGS:
        return explicit

    env = os.environ.get("PHOTO_S_LANG")
    if env in SUPPORTED_LANGS:
        return env

    if use_config:
        try:
            from .config import find_config, load_config
            cfg = load_config(config_path or find_config())
            from .config import config_language
            lang = config_language(cfg)
            if lang in SUPPORTED_LANGS:
                return lang
        except Exception:
            pass

    if use_persisted:
        lang = load_persisted_language()
        if lang in SUPPORTED_LANGS:
            return lang

    return _system_language()


# ── GUI persistence (~/.photos/language) ───────────────────────────────────

STATE_DIR = Path.home() / ".photos"
LANGUAGE_FILE = STATE_DIR / "language"


def load_persisted_language() -> Optional[str]:
    """Read the persisted GUI language choice; None when absent/invalid."""
    try:
        if not LANGUAGE_FILE.is_file():
            return None
        lang = LANGUAGE_FILE.read_text(encoding="utf-8").strip()
        return lang if lang in SUPPORTED_LANGS else None
    except Exception:
        return None


def save_language(lang: str) -> None:
    """Persist the GUI language choice. Never crashes the GUI on OSError."""
    if lang not in SUPPORTED_LANGS:
        return
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        LANGUAGE_FILE.write_text(lang + "\n", encoding="utf-8")
    except OSError:
        pass


# ── CLI string table ───────────────────────────────────────────────────────

# CLI strings, one key per translatable string. zh/en key sets MUST be
# identical (parity enforced by tests/test_i18n.py). Only named ``{placeholders}``
# are allowed — no positional ``{}`` (they'd crash ``_t`` on .format()).

STRINGS = {
    "zh": {
    "eta_remaining": "剩余",
        "msg_full_edition": "完整版",
        "msg_verb_keep_sharpest": "保留最清晰并删除其余",
        "msg_done_delete": "已删除 {removed} 个文件, 保留 {kept} 个",
        "msg_done_keep": "已保留最清晰并移除 {removed} 个, 保留 {kept} 个",
        "msg_done_move": "已移动 {removed} 个文件, 保留 {kept} 个",
        "msg_will_delete": "将删除 {removed} 个文件, 保留 {kept} 个",
        "msg_will_keep": "将保留最清晰并移除 {removed} 个, 保留 {kept} 个",
        "msg_will_move": "将移动 {removed} 个文件, 保留 {kept} 个",
        "msg_dupes_savings": "共 {n} 个重复文件, 可节省 {savings:,} 字节",
        "msg_dupes_total": "共 {n} 个重复文件, 可节省 {savings:,} 字节",
        "msg_actual": "实际",
        "msg_auto_tune": "（自动调优）",
        "msg_bad_jobs": "❌ --jobs 格式应为逗号分隔的数字。",
        "msg_bench_files": "bench: {n} files in {dir}",
        "msg_bench_tip": "提示: 在真实照片集上跑，用结果决定并发数",
        "msg_blurfaces_done": "✅ 已模糊 {ok}/{n} 张图片的人脸",
        "msg_cancel_input_err": "已取消（无法读取确认输入）",
        "msg_cancelled": "已取消",
        "msg_ceiling": "（上限）",
        "msg_check_corrupt": "损坏",
        "msg_check_done": "📊 检查完成",
        "msg_check_ok": "通过",
        "msg_config_created": "✅ 已创建配置文件",
        "msg_config_file": "📋 配置文件:",
        "msg_config_hint": "在此文件设置默认值, 之后用 --config 指定或放在工作目录自动生效",
        "msg_config_init_hint": "用 `photo-s config init` 创建",
        "msg_config_load_err": "❌ 配置文件加载失败",
        "msg_confirm_continue": "确认继续? [y/N]: ",
        "msg_confirm_dedup": "\n⚠️  即将{verb} {n} 个文件. 确认? [y/N]: ",
        "msg_confirm_delete": "⚠️  警告: 将删除 {n} 个原始文件！",
        "msg_contact_sheet": "✅ 已生成联系表",
        "msg_copy_to": "📁 将复制到",
        "msg_cull_kept": "📊 通过",
        "msg_select_summary": "精选 {kept} · 淘汰 {rejected} · 移动 {moved}",
        "msg_date_label": "日期",
        "msg_dir_not_found": "❌ 目录不存在",
        "msg_done": "[{n}/{n}] 完成!",
        "msg_dry_run": "🔍 预览模式 — 不会实际操作",
        "msg_dry_run_settings": "🔍 预览模式 — 不会实际处理文件",
        "msg_dup_groups": "📊 发现 {n} 组重复",
        "msg_editing_exif": "📝 编辑EXIF标签",
        "msg_eval_no_output": "无可比对输出",
        "msg_exif_done": "✅ EXIF编辑完成",
        "msg_exif_label": "保留EXIF",
        "msg_expected": "期望",
        "msg_failed": "失败",
        "msg_files_found": "📁 找到 {n} 个图片文件:",
        "msg_files_suffix": "个文件",
        "msg_formats_title": "PhotoS — 支持的图片格式",
        "msg_gallery": "✅ 已生成 Gallery",
        "msg_group_files": "组 #{i}: {n} 个文件",
        "msg_hashing_progress": "计算哈希",
        "msg_hashing_start": "🔍 计算哈希 {n} 个文件...",
        "msg_hdr_done": "✅ 已用 {n} 张曝光合成 HDR → {out}",
        "msg_heic_hint": "💡 提示: 安装 pillow-heif 可获得跨平台 HEIC 支持",
        "msg_images_count": "{n} 张图",
        "msg_input_formats": "输入格式（可读取）",
        "msg_installed_plugins": "已装插件",
        "msg_jobs_ints": "❌ --jobs 需要 >= 1 的整数。",
        "msg_lossless_note": "⚠️  注意: {fmt} 是无损/弱压缩格式，目标体积控制效果有限。",
        "msg_manifest": "清单",
        "msg_manifest_written": "✅ 清单已写入",
        "msg_mcp_py310": "❌ photo-s mcp 需要 Python 3.10+（mcp SDK 要求）",
        "msg_mismatched": "⚠️  不匹配",
        "msg_missing": "❌ 缺失",
        "msg_no": "否",
        "msg_no_config": "⚠️  未找到配置文件。",
        "msg_no_custom_opts": "（无自定义选项）",
        "msg_no_dupes": "✅ 未发现重复图片。",
        "msg_no_exif_tags": "❌ 请指定至少一个EXIF标签，或 --show 读取。",
        "msg_no_files": "❌ 没有找到文件。",
        "msg_no_gui": "本构建未包含 GUI（lite 版）。",
        "msg_no_images": "❌ 没有找到支持的图片文件。",
        "msg_no_images_dir": "❌ 目录里没有找到支持的图片文件。",
        "msg_no_presets": "📋 暂无预设",
        "msg_no_lens_profiles": "📋 暂无镜头档案（用 `photo-s lens-profile save NAME --distort K1 ...` 创建）",
        "msg_none": "（无）",
        "msg_optimize_label": "优化",
        "msg_optional_features": "可选依赖",
        "msg_output_dir_label": "输出目录",
        "msg_output_formats": "输出格式（可写入）",
        "msg_preset_deleted": "✅ 预设已删除",
        "msg_preset_not_found": "❌ 预设不存在",
        "msg_preset_not_found_generic": "❌ 预设不存在: {name}",
        "msg_lens_profile_saved": "✅ 镜头档案已保存",
        "msg_lens_profiles_available": "📋 可用镜头档案（--lens-profile NAME 套用）",
        "msg_lens_profile_deleted": "✅ 镜头档案已删除",
        "msg_lens_profile_not_found": "❌ 镜头档案不存在",
        "msg_preset_saved": "✅ 预设已保存",
        "msg_preset_captured": "已捕获 {n} 项非默认设置",
        "msg_preset_apply_hint": "批量套用: photo-s batch <files> --preset {name}",
        "msg_preset_show": "📋 预设 '{name}':",
        "msg_presets_available": "📋 可用预设:",
        "msg_processing": "[{i}/{n}] 处理中 {name}...",
        "msg_profile_start": "📦 预设 {name} ({n} files)",
        "msg_profiles_need": "❌ --profiles 需要至少一个预设名",
        "msg_quality": "质量",
        "msg_quality_label": "质量",
        "msg_quality_max": "质量上限",
        "msg_quality_range": "质量范围: 5–{q}",
        "msg_rating_label": "星级",
        "msg_rename_in_place": "📝 将就地改名",
        "msg_rename_note": "（注意: 就地改名会替换原文件名）",
        "msg_rename_ok": "📊 成功 {ok}/{total} files",
        "msg_report_err": "❌ 报告写入失败",
        "msg_report_profiles_conflict": "⚠️  --report 与 --profiles 不能同时使用; 已跳过 report",
        "msg_resize_label": "缩放",
        "msg_same_as_source": "（与源文件相同）",
        "msg_saved": "节省",
        "msg_scale_label": "缩放",
        "msg_scanning": "🔍 正在扫描 {n} 个文件...",
        "msg_settings_applied": "将应用的设置",
        "msg_start_processing": "🚀 开始处理...",
        "msg_subfolder": "子文件夹",
        "msg_success": "成功",
        "msg_summary": "处理完成",
        "msg_synced_date": "✅ 已用文件修改时间写入拍摄日期 (DateTimeOriginal ← mtime)",
        "msg_target_format": "目标格式",
        "msg_target_size": "目标体积",
        "msg_target_size_header": "🎯 目标体积: ≤ {size}",
        "msg_token_generated": "🔐 已生成 token",
        "msg_total_compressed": "压缩后总大小",
        "msg_total_ok": "共 {total} 项, 一致 {ok}",
        "msg_total_original": "原始总大小",
        "msg_verb_delete": "删除",
        "msg_verb_move": "移入重复文件夹",
        "msg_verb_report": "报告",
        "msg_write_failed": "⚠️  {n} 个文件写入失败",
        "msg_written_csv": "✅ 已从CSV写入元数据",
        "msg_written_json": "✅ 已从JSON写入元数据",
        "msg_yes": "是",
        "val_format": "无效格式 invalid format: '{fmt}'。可选 Choose from: {formats}",
        "val_size": "无效的大小格式 invalid size format: '{val}'。支持的格式 supported: {supported}",
        "help___directory": "要监视的文件夹",
        "help___files": "图片文件或通配符",
        "help___image_files": "图片文件",
        "help___paths": "文件/目录/通配符",
        "help___plugin_name": "插件名",
        "help___plugin_name_alnum": "插件名（字母数字连字符）",
        "help___plugin_name_eg": "插件名（如 scunet）",
        "help___preset_name": "预设名称",
        "help___preset": "套用已保存的预设作为基础选项（显式命令行参数优先）",
        "cmd_batch": "批量处理（压缩+转换+缩放）",
        "cmd_bench": "批量处理基准测试",
        "cmd_analyze": "感知分析（直方图/色彩/曝光，agent 调色反馈闭环）",
        "help___sample_size": "采样最大边长（默认 256，越小越快）",
        "help___grid": "区域反馈网格（4/8 → 逐格亮度/色偏 + 天空/肤色/过曝区域框）",
        "cmd_lr_scan": "Lightroom 数据桥接报告（自动发现 catalog/XMP，覆盖报告 + 训练数据导出）",
        "help___lr_paths": "catalog(.lrcat)/XMP(.xmp) 文件或目录；缺省自动扫 ~/Pictures 与 ~/Desktop",
        "help___lr_export": "导出训练数据 JSONL（每行: path + PhotoS 参数）到此目录",
        "help___lr_render": "渲染已编辑照片的 before 图（rawpy 默认显影 → JPEG）到此目录",
        "help___lr_sanitize": "导出脱敏：path/image 只留 basename（隐藏本地目录结构），映射写 lr_paths.json",
        "cmd_lr_train": "训练自动基调模型（岭回归，纯 numpy，9 项全局参数）",
        "help___lr_data": "lr_records.jsonl（lr-scan --export-dir 产出）",
        "help___lr_images": "before 图目录（--render-dir 产出；缺省用 JSONL 内 image 键）",
        "help___lr_out": "输出文件（默认 auto_tone.npz / eval.json）",
        "help___lr_lambda": "岭回归正则强度（默认 1.0）",
        "cmd_lr_predict": "自动基调推理：图片 → 9 项全局参数",
        "help___lr_image": "输入图片路径",
        "help___lr_model": "模型文件（lr-train 岭回归或 CLIP+MLP npz，默认 auto_tone.npz）",
        "cmd_lr_recipes": "编辑配方聚类（个人风格配方库，KMeans 参数空间）",
        "help___lr_k": "簇数（默认 6）",
        "cmd_lr_similar": "相似修图检索（内容特征 kNN → 既往修图起点）",
        "cmd_lr_eval": "教师评测集准备（采样 → before/after 渲染对 + 打分模板）",
        "help___lr_sample": "采样张数（默认 200）",
        "cmd_lr_merge": "合并多机数据包（去重 + before 图集 + 溯源）",
        "help___lr_packages": "各电脑数据包目录（含 lr_records.jsonl [+before/]）",
        "help___lr_merge_out": "合并输出目录（默认 merged/）",
        "msg_lr_merged": "合并 {p} 个数据包: {n} 条记录（已编辑 {e}），复制 {img} 张 before 图 → {out}",
        "msg_lr_rendered": "before 图渲染: {n} 张（跳过 {skip}，失败 {failed}）",
        "msg_lr_trained": "模型训练完成: {n} 样本, R²={r2}, 已存 {out}",
        "msg_lr_recipes": "配方聚类: {k} 簇 / {n} 样本",
        "msg_lr_similar": "相似修图（{img}）→ top {k}",
        "msg_lr_eval": "评测集: {n} 对（before/after），已存 {out}",
        "cmd_diff": "版本对比（PSNR/SSIM/平均绝对差，before/after 数值化）",
        "help___diff_paths": "两张图片路径（before after）",
        "cmd_audit": "出片质量闸门（pass/fail + 原因，agent 终止条件）",
        "cmd_suggest": "规则型参数推荐（analyze → 建议参数，零模型）",
        "help___scale": "建议幅度缩放 0-1（温和=0.5）",
        "suggest_neutral": "指标中性——没有客观可修项",

        "help___over_max": "过曝像素占比上限（百分比，默认 2.0）",
        "help___under_max": "欠曝像素占比上限（百分比，默认 2.0）",
        "help___blur_min": "模糊分下限（默认 0.05）",
        "help___aesthetic": "美学闸门：分数下限 1-10（需 auto-tone 插件：SigLIP 头或 qwen extra）",
        "cmd_preview": "视觉快照（缩放 JPEG base64 + 直方图 PNG，多模态 agent 的眼睛）",
        "help___max_dim": "最长边上限（默认 1024）",
        "help___no_histogram": "不输出直方图 PNG",
        "help___trace": "轨迹日志目录（每文件一行: before-analyze → params → after-analyze，训练数据格式）",
        "help___write_xmp": "为每个输入写 .xmp sidecar（Lightroom 打开原图即可续修；与 --auto-tone 组合时导出真实预测参数）",
        "cmd_xmp_export": "写出 Lightroom XMP sidecar（调整 + 评分/关键词，LR 可直接续修）",
        "help_xmp_preset": "从指定预设取调整值",
        "help_xmp_options": "JSON 对象形式的调整字段（如 '{\"ev\":0.3}'）",
        "help_xmp_rating": "写入 xmp:Rating 0-5",
        "help_xmp_keywords": "逗号分隔关键词（dc:subject，LR 关键词面板可见）",
        "help_xmp_title": "图像标题（dc:title）",
        "help_xmp_out_dir": "sidecar 输出目录（默认写在原图旁）",
        "msg_xmp_no_source": "未指定任何调整来源或元数据（--preset/--options/--auto-tone/--rating/--keywords/--title 任选其一）",
        "cmd_autopilot": "无人值守修图管线（监视 → suggest/auto-tone → audit → passed/review 分流）",
        "help_autopilot_dir": "监视目录",
        "help_autopilot_out_dir": "输出根目录（默认 <监视目录>/photo-s-out/，含 passed/ review/ 与 autopilot.jsonl）",
        "help_autopilot_mode": "参数来源：suggest（零模型规则）/ auto_tone（需插件）/ both（先修偏再上风格）",
        "help_autopilot_scan_existing": "启动时先处理目录中已有图片（默认只处理新落入的）",
        "help_autopilot_log": "JSONL 轨迹路径（默认 <输出根>/autopilot.jsonl）",
        "msg_autopilot_start": "🚁 无人值守启动 Watching {dir}（mode={mode}）→ {out}",
        "msg_autopilot_stopped": "🚁 已停止 Stopped.",
        "cmd_index": "建语义索引（embed 插件 = SigLIP 文本+图像；无插件 = 内置 84 维仅图像）",
        "help_index_index": "索引文件路径（默认 <公共根>/.photo-s-index.npz）",
        "help_index_rebuild": "忽略旧索引全量重建",
        "help_index_extractor": "指定抽取器名（默认自动：embed 插件优先）",
        "help_index_tags": "建索引后自动打标：逗号分隔标签（需文本编码器；写入 EXIF keywords）",
        "help_index_min_score": "打标保留的余弦下限（默认 0.2）",
        "help_index_max_tags": "每图最多标签数（默认 5）",
        "help_index_write_xmp": "标签同时写 XMP dc:subject sidecar（LR 关键词面板可见）",
        "msg_index_done": "索引完成：{total} 张（新增 {indexed} / 复用 {kept} / 移除 {removed}，{extractor}）→ {index}",
        "msg_index_tags": "自动打标：{n} 张已写入标签",
        "cmd_find": "语义搜索：文本查询或以图搜图（对 index 建的库）",
        "help_find_query": "自然语言查询（建议英文——SigLIP 以英文语料为主；需 embed 插件）",
        "help_find_image": "以图搜图：参考图路径",
        "help_find_index": "索引文件路径（默认 ./.photo-s-index.npz）",
        "help_find_k": "返回前 K 条（默认 10）",
        "help_find_min_score": "过滤下限余弦分",
        "msg_find_no_query": "缺查询：给文本查询或 --image 参考图",
        "msg_lr_header": "Lightroom 覆盖报告（{sec}s）",
        "msg_lr_catalogs": "目录        照片   已编辑  AI蒙版  点颜色",
        "msg_lr_summary": "合计 {photos} 张 + XMP {xmp} 张，已编辑 {edited} 张",
        "msg_lr_params": "── 全局参数使用频率（top 15）──",
        "msg_lr_masks": "── 蒙版类型分布 ──",
        "msg_lr_tools": "── 工具调用轨迹（top 12）──",
        "msg_lr_unmapped": "── 未映射缺口 ──",
        "msg_lr_export": "训练数据已导出: {path}",
        "cmd_blurfaces": "检测并模糊/打码人脸（隐私保护）",
        "cmd_check": "检查图片完整性",
        "cmd_commands": "可用命令",
        "cmd_compress": "压缩图片体积",
        "cmd_config": "管理配置文件",
        "cmd_contact_sheet": "生成联系表",
        "cmd_convert": "转换图片格式",
        "cmd_cull": "曝光/清晰度筛选",
        "cmd_dedup": "查找重复图片",
        "cmd_delete": "删除预设",
        "cmd_exif": "批量读写EXIF元数据/打标/筛选",
        "cmd_fetch": "预下载模型权重",
        "cmd_gallery": "生成 HTML 画廊",
        "cmd_hash": "生成/校验校验和清单",
        "cmd_hdr": "包围曝光合并 HDR（曝光融合）",
        "cmd_info": "显示支持的格式列表",
        "cmd_init": "创建默认配置文件",
        "cmd_install": "安装官方插件",
        "cmd_list": "列出已装/可用插件",
        "cmd_load": "加载预设",
        "cmd_lens_profile": "管理镜头档案（save/list/delete）",
        "cmd_mcp": "启动 MCP server（供 AI agent / Claude Desktop 调用）",
        "cmd_plugin": "管理官方插件",
        "cmd_preset": "管理预设配置",
        "cmd_rename": "批量重命名图片",
        "cmd_save": "保存预设",
        "cmd_scaffold": "生成插件开发脚手架",
        "cmd_select": "按评分分拣精选/淘汰照片",
        "cmd_serve": "启动 REST API 服务（供 AI agent 调用）",
        "cmd_show": "显示生效配置",
        "cmd_uninstall": "卸载插件",
        "cmd_watch": "监视文件夹自动处理",
        "desc": "PhotoS — 批量图片压缩与格式转换工具",
        "epilog": "使用示例 Examples:\n  photo-s compress *.jpg -q 80                 批量压缩JPEG图片\n  photo-s compress *.jpg --target-size 500KB   自动调优质量至500KB以内\n  photo-s compress *.ARW -q 90                 将RAW照片转为JPEG\n  photo-s compress *.ARW --raw-half-size -q 85  RAW半尺寸快速处理\n  photo-s convert *.png -f webp -q 85          转换PNG为WebP\n  photo-s batch ~/Pictures/ -r -f JPEG -q 70   递归处理整个目录\n  photo-s batch ~/Pictures/ -r --target-size 2MB  自动调优质量至2MB以内\n  photo-s batch . --resize 1920x1080           批量缩放图片\n  photo-s batch . --scale 50                   缩小到50%\n  photo-s batch . --no-exif                    不保留EXIF信息\n  photo-s batch . --dry-run                    预览模式（不实际处理）\n  photo-s batch . --organize date              按日期创建子文件夹\n  photo-s batch . --organize date-camera       按日期+相机创建子文件夹",
        "help___action": "操作: report=仅报告, move=移到_duplicates文件夹, delete=删除, keep-sharpest=连拍保留最清晰（默认 report）",
        "help___aperture": "光圈 f-number，如 '2.8' 或 'f/2.8'",
        "help___artist": "作者",
        "help___auto_exposure": "自动曝光: 均值亮度归一化到目标",
        "help___auto_levels": "自动色阶",
        "help___auto_straighten": "自动扶正地平线（需 photo-s-tools[enhance]）",
        "help___bg": "背景色",
        "help___blur_faces": "检测并模糊人脸: blur=高斯模糊, pixelate=马赛克（需 photo-s-tools[enhance]）",
        "help___blur_faces_margin": "人脸框外扩百分比（默认 20）",
        "help___cutout": "抠图/背景移除: subject|person|object:类别|color:R,G,B[,tol=30][,feather=0][,invert]（透明需 PNG/WebP/TIFF/AVIF/HEIC 输出；JPEG 按文件报错）",
        "help___blur_score": "计算输入图模糊度评分",
        "help___brightness": "亮度（1.0 = 不变）",
        "help___camera": "筛选: 相机型号子串",
        "help___caption": "图注",
        "help___clarity": "清晰度: 大半径局部对比（LR clarity）",
        "help___color_grading": "三向颜色分级: 各档 “zone:hue,sat”（shadows/midtones/highlights）",
        "help___cols": "每行列数（默认 4）",
        "help___config": "配置文件路径",
        "help___contrast": "对比度（1.0 = 不变）",
        "help___copyright": "版权",
        "help___crop": "裁剪，如 800x600+100+50（偏移可省 → 居中）",
        "help___curves": "点曲线: 每通道控制点 “ch:x,y;x,y|ch:...”（PCHIP 单调样条）",
        "help___crop_ratio": "按比例居中裁剪",
        "help___date": "拍摄日期",
        "help___dehaze": "去雾: 暗通道先验（负数加雾）",
        "help___date_from": "筛选: 起始日期（配合 --show）",
        "help___date_from_mtime": "用文件修改时间写拍摄日期（反向同步）",
        "help___date_shift": "EXIF 日期偏移，如 \"-5h30m\" / \"+2h\" / \"1d\"",
        "help___date_to": "筛选: 结束日期（配合 --show）",
        "help___auto_tone": "AI 自动调色强度 0-1（需 auto-tone 插件；零模型替代：photo-s suggest）",
        "help___denoise": "降噪强度 NLM 3-20（需 photo-s-tools[enhance]）",
        "help___desc": "描述",
        "help___description": "图片描述",
        "help___dir": "生成目录（默认 plugins/<name>）",
        "help___dry_run": "预览模式，不实际处理",
        "help___ev": "曝光补偿",
        "help___evaluate": "计算SSIM质量评分",
        "help___export_sharpen": "导出锐化 0-2（LR 式输出级 USM，半径随输出分辨率缩放；0 关闭）",
        "help___flatten_cmyk": "CMYK 输入转 RGB",
        "help___highlight_recovery": "高光恢复 0-1（LR 式：压平硬切高光，恢复出渐变细节；0 关闭）",
        "help___flip": "镜像翻转：h 水平 / v 垂直",
        "help___focal": "焦距",
        "help___format": "目标格式（默认 JPEG，大小写不敏感）",
        "help___from_csv": "从 CSV 批量写入元数据（首列 path）",
        "help___from_json": "从 JSON 批量写入元数据",
        "help___gamma": "伽马（1.0 = 不变，显示亮度）",
        "help___gps": "GPS 坐标 '纬度,经度'（批量写入全部文件，如 31.23,121.47）",
        "help___gpx_trace": "按 GPX 轨迹注入 GPS 坐标（按 EXIF 拍摄时间匹配）",
        "help___grain": "颗粒: 胶片感亮度加权噪点 “amount[,size]”",
        "help___masks": "局部蒙版: “name:type:params;...”，type=linear（x0,y0,x1,y1）/radial（cx,cy,rx,ry 四参）/color（r,g,b,tol=）/subject/person/object:类名/brush:x,y,r|x,y,r（负点 -x,y,r 减）/combo:A&B|A-B，相对坐标 0-1；段尾可加 ,feather=/,invert（如 “sky:linear:0.5,0,0.5,1,feather=0.3”）",
        "help___mask_adjust": "蒙版内局部调整: “name:key=value,...;...”，key=exposure/brightness/contrast/saturation/vibrance/clarity/texture/sharpen/temp/tint/blur 或字符串键 curves/hsl/color_grading/vignette/grain（大括号包裹，同全局格式）",
        "help___lens_distort": "镜头畸变矫正: k1 径向系数，正=矫正桶形（边缘外拉），负=枕形",
        "help___lens_vignette": "去镜头暗角: “amount[,midpoint]” 提亮边角",
        "help___lens_ca": "消色差: “r_scale,b_scale” 通道径向缩放（如 “0.999,1.001”）",
        "help___lens_profile": "套用命名镜头档案（lens-profile save 维护；显式 lens_* 参数优先）",
        "help___lens_profile_name": "镜头档案名（如 “RF 24-70mm f/2.8”）",
        "help___grayscale": "转为黑白",
        "help___hdr_align": "手持包围曝光对齐（AlignMTB，消除鬼影）",
        "help___host": "监听地址（默认 127.0.0.1）",
        "help___hsl": "HSL 分色: 8 色域偏移 “color:h,s,l;...”",
        "help___point_color": "点颜色: 取样色定向调整 “r,g,b:hue,sat,lum[,range];...”，mask 以取样色为中心",
        "help___images": "最多取前 N 张图（默认全部）",
        "help___iso": "感光度",
        "help___jobs": "并行处理线程数（默认 auto，即 min(CPU核数, 8)）",
        "help___jpeg_subsampling": "JPEG 色度子采样 444/422/420（默认 420；444 保留全色彩，体积更大）",
        "help___json": "输出 JSON 格式（供 AI agent 调用）",
        "help___keep_min": "精选阈值: 评分 ≥ N 移入精选目录（默认 4）",
        "help___keep_mtime": "保留原始文件修改时间",
        "help___keywords": "关键词，逗号分隔（写模式=赋值；--show 下=任意命中筛选）",
        "help___language": "界面语言（默认 auto，跟随系统）",
        "help___lens": "镜头型号",
        "help___levels": "手动色阶: “black,white[,gamma]”",
        "help___cull_score": "质量评分模式（0-100 加权分排序，不做淘汰）",
        "help___cull_burst": "连拍分组：EXIF 时间聚类，每组只留最高分（常与 --score 连用）",
        "help___cull_gap": "连拍分组间隔秒数（默认 2.0）",
        "msg_cull_burst_best": "连拍组保留最佳",
        "help___list": "仅输出匹配文件路径（供管道）",
        "help___list_tools": "列出工具与参数 schema（不启动服务器）",
        "help___log_curve": "LOG/平面文件还原曲线 (SLOG3 CLOG3 LOGC3 DLOG VLOG HLG)",
        "help___luminance_max": "筛选: 最高平均亮度",
        "help___luminance_min": "筛选: 最低平均亮度",
        "help___lut": "3D/1D .cube LUT 调色（内置三线性；装 photo-s-plugin-lut 后 用四面体插值 + 电影预设）",
        "help___make": "相机厂商（如 SONY）",
        "help___max_pixels": "最长边像素上限，如 8000。仅缩小",
        "help___max_straighten_angle": "扶正最大允许倾斜角（默认 10°）",
        "help___model": "相机型号（如 ILCE-7M4）",
        "help___no_auto_rotate": "禁用自动旋转",
        "help___no_exif": "不保留EXIF元数据",
        "help___no_optimize": "禁用PIL优化",
        "help___organize": "按模板创建子文件夹。预设: date, camera, date-camera。自定义: {year}/{month}/{camera} 等",
        "help___output": "输出文件路径",
        "help___output_dir": "输出目录（默认: 与源文件相同）",
        "help___overexposed_max": "筛选: 过曝像素上限 %%（默认: 不筛选）",
        "help___overwrite": "覆盖已存在的文件",
        "help___pad": "留白补边到目标比例",
        "help___pad_bg": "留白背景色（默认 #000000）",
        "help___path": "输出路径（默认 ./photo-s.toml）",
        "help___pattern": "命名模板，变量: {year} {month} {day} {date} {time} {camera} {make} {original} {iso} {focal} {seq}",
        "help___port": "监听端口（默认 8787）",
        "help___prefix": "输出文件名前缀",
        "help___print_size": "打印尺寸，如 8x10@300dpi（中心裁剪+精确像素）",
        "help___profiles": "按预设多跑: 同一批文件按每个预设各输出一份（预设名逗号分隔，如 web,thumb）",
        "help___progressive": "使用渐进式JPEG",
        "help___quality": "输出质量 1-100（默认 85）",
        "help___rating": "星级 0-5（写模式=赋值；--show 下=精确筛选）",
        "help___rating_min": "筛选: 最低星级",
        "help___raw_half_size": "RAW 文件半尺寸解码（更快）",
        "help___raw_no_auto_bright": "禁用RAW自动亮度",
        "help___raw_demosaic": "RAW 去马赛克算法 auto/ahd/vng/ppg/dcb/dht/amaze（默认 auto；amaze 质量最高最慢）",
        "help___raw_color_space": "RAW 输出色彩空间 sRGB/AdobeRGB/ProPhotoRGB（默认 sRGB 自动打 ICC；宽色域输出不加标记，需在编辑器打）",
        "help___raw_16bit": "RAW 16-bit 解码；TIFF 输出写 16-bit（需 tifffile；仅纯转换有意义，JPEG 仍是 8-bit）",
        "help___ready_file": "监听成功后写入 {port, token, pid} 握手文件（供宿主 agent 读取）",
        "help___recursive": "递归搜索目录",
        "help___reject_max": "淘汰阈值: 评分 ≤ N 移入淘汰目录（默认 2）",
        "help___remove_original": "处理后删除原文件",
        "help___rename": "智能重命名，变量: {year} {month} {day} {date} {time} {camera} {make} {original} {iso} {focal} {seq}",
        "help___report": "导出 CSV 处理报告",
        "help___resize": "缩放尺寸",
        "help___resume": "跳过输出已存在的文件（断点续跑）",
        "help___rotate": "任意角度旋转（正数 = 顺时针）",
        "help___rotate_bg": "旋转背景填充色（默认 black）",
        "help___saturation": "饱和度（1.0 = 不变）",
        "help___scale": "缩放百分比",
        "help___scrub": "清除全部元数据（EXIF+ICC+注释）",
        "help___select_copy": "复制而非移动（保留原文件）",
        "help___selects_dir": "精选目录: 高分照片移到这里",
        "help___rejects_dir": "淘汰目录: 低分照片移到这里",
        "help___sepia": "复古色调",
        "help___sha256": "使用 SHA-256（默认）",
        "help___sharpen": "锐化（1.0 = 不变）",
        "help___sharpness_min": "筛选: 最低清晰度分",
        "help___show": "读取模式: 显示元数据并按条件筛选",
        "help___shutter": "快门速度，如 '1/250' 或 '2'（秒）",
        "help___sizes": "多尺寸输出",
        "help___software": "软件",
        "help___srgb": "输出标记 sRGB 色彩配置文件",
        "help___strip_gps": "移除GPS位置信息",
        "help___suffix": "输出文件名后缀（默认 _compressed）",
        "help___sync_date": "输出时间设为 EXIF 拍摄时间",
        "help___target_size": "目标文件体积，如 500KB、2MB。自动调整质量以适应该大小",
        "help___texture": "纹理: 小半径局部对比（LR texture）",
        "help___threshold": "汉明距离阈值（默认 5，越小越严格）",
        "help___thumb": "缩略图尺寸（默认 240x240）",
        "help___title": "标题",
        "help___token": "Bearer token 认证；auto = 随机生成",
        "help___underexposed_max": "筛选: 欠曝像素上限",
        "help___verify": "校验模式: 重新哈希并对照清单",
        "help___version": "显示版本号",
        "help___vibrance": "自然饱和度: 按当前饱和度反向加权 [-1,1]",
        "help___vignette": "暗角: 径向渐变 “amount[,midpoint[,feather]]”",
        "help___watermark_image": "图片水印路径",
        "help___watermark_opacity": "水印透明度",
        "help___watermark_pos": "水印位置",
        "help___watermark_text": "文字水印",
        "help___wb": "白平衡色温",
        "help___wb_from": "从参考图取样白平衡",
        "help___wb_tint": "白平衡 tint: 绿(-)/品红(+) G-M 轴 [-100,100]",
        "help___yes": "跳过所有确认提示",
    },
    "en": {
        "eta_remaining": "remaining",
        "msg_full_edition": "Full edition",
        "msg_verb_keep_sharpest": "keep the sharpest and delete the rest",
        "msg_done_delete": "Deleted {removed} file(s), kept {kept}",
        "msg_done_keep": "Kept the sharpest, removed {removed}, kept {kept}",
        "msg_done_move": "Moved {removed} file(s), kept {kept}",
        "msg_will_delete": "Will delete {removed} file(s), keep {kept}",
        "msg_will_keep": "Will keep the sharpest, remove {removed}, keep {kept}",
        "msg_will_move": "Will move {removed} file(s), keep {kept}",
        "msg_dupes_savings": "{n} duplicate file(s), {savings:,} bytes recoverable",
        "msg_dupes_total": "{n} duplicate file(s), {savings:,} bytes recoverable",
        "msg_actual": "actual",
        "msg_auto_tune": "(auto-tune)",
        "msg_bad_jobs": "❌ Bad --jobs list.",
        "msg_bench_files": "bench: {n} files in {dir}",
        "msg_bench_tip": "Tip: run on real photos; use results to pick concurrency",
        "msg_blurfaces_done": "✅ Blurred faces in {ok}/{n} images",
        "msg_cancel_input_err": "Cancelled (could not read confirmation input)",
        "msg_cancelled": "Cancelled.",
        "msg_ceiling": "(ceiling)",
        "msg_check_corrupt": "corrupt",
        "msg_check_done": "📊 Check complete",
        "msg_check_ok": "OK",
        "msg_config_created": "✅ Created config file",
        "msg_config_file": "📋 Config file:",
        "msg_config_hint": "Set defaults here, then pass --config or place in the working dir",
        "msg_config_init_hint": "Create with `photo-s config init`",
        "msg_config_load_err": "❌ Config load error",
        "msg_confirm_continue": "Confirm? [y/N]: ",
        "msg_confirm_dedup": "\n⚠️  About to {verb} {n} file(s). Confirm? [y/N]: ",
        "msg_confirm_delete": "⚠️  Warning: {n} original file(s) will be deleted!",
        "msg_contact_sheet": "✅ Generated contact sheet",
        "msg_copy_to": "📁 Copy to",
        "msg_cull_kept": "📊 Kept",
        "msg_select_summary": "keep {kept} · reject {rejected} · moved {moved}",
        "msg_date_label": "date",
        "msg_dir_not_found": "❌ Directory not found",
        "msg_done": "[{n}/{n}] Done!",
        "msg_dry_run": "🔍 Dry run — no files will be changed",
        "msg_dry_run_settings": "🔍 Dry run — no files will be modified",
        "msg_dup_groups": "📊 Found {n} duplicate group(s):",
        "msg_editing_exif": "📝 Editing EXIF tags",
        "msg_eval_no_output": "no comparable outputs",
        "msg_exif_done": "✅ EXIF editing done.",
        "msg_exif_label": "EXIF",
        "msg_expected": "expected",
        "msg_failed": "Failed",
        "msg_files_found": "📁 Found {n} image file(s):",
        "msg_files_suffix": "file(s)",
        "msg_formats_title": "PhotoS — Supported Formats",
        "msg_gallery": "✅ Generated Gallery",
        "msg_group_files": "Group #{i}: {n} files",
        "msg_hashing_progress": "Hashing",
        "msg_hdr_done": "✅ HDR merged from {n} exposures → {out}",
        "msg_hashing_start": "🔍 Hashing {n} file(s)...",
        "msg_heic_hint": "💡 Tip: install pillow-heif for cross-platform HEIC support",
        "msg_images_count": "{n} images",
        "msg_input_formats": "Input (can read):",
        "msg_installed_plugins": "Installed plugins:",
        "msg_jobs_ints": "❌ --jobs needs ints >= 1.",
        "msg_lossless_note": "⚠️  Note: {fmt} is lossless/weakly-compressed; target-size control is limited.",
        "msg_manifest": "Manifest",
        "msg_manifest_written": "✅ Manifest written",
        "msg_mcp_py310": "❌ photo-s mcp requires Python 3.10+ (mcp SDK requirement)",
        "msg_mismatched": "⚠️  Mismatched",
        "msg_missing": "❌ Missing",
        "msg_no": "No",
        "msg_no_config": "⚠️  No config file found.",
        "msg_no_custom_opts": "(no custom options)",
        "msg_no_dupes": "✅ No duplicates found.",
        "msg_no_exif_tags": "❌ Specify at least one EXIF tag, or use --show to read.",
        "msg_no_files": "❌ No files found.",
        "msg_no_gui": "This build has no GUI (lite edition).",
        "msg_no_images": "❌ No supported image files found.",
        "msg_no_images_dir": "❌ No images found in directory.",
        "msg_no_presets": "📋 No presets saved yet.",
        "msg_no_lens_profiles": "📋 No lens profiles yet (create one: `photo-s lens-profile save NAME --distort K1 ...`)",
        "msg_none": "(none)",
        "msg_optimize_label": "Optimize",
        "msg_optional_features": "Optional features:",
        "msg_output_dir_label": "Output dir",
        "msg_output_formats": "Output (can write):",
        "msg_preset_deleted": "✅ Preset deleted",
        "msg_preset_not_found": "❌ Preset not found",
        "msg_preset_not_found_generic": "❌ Preset not found: {name}",
        "msg_lens_profile_saved": "✅ Lens profile saved",
        "msg_lens_profiles_available": "📋 Available lens profiles (apply with --lens-profile NAME)",
        "msg_lens_profile_deleted": "✅ Lens profile deleted",
        "msg_lens_profile_not_found": "❌ Lens profile not found",
        "msg_preset_saved": "✅ Preset saved",
        "msg_preset_captured": "captured {n} non-default settings",
        "msg_preset_apply_hint": "apply in batch: photo-s batch <files> --preset {name}",
        "msg_preset_show": "📋 Preset '{name}':",
        "msg_presets_available": "📋 Available presets:",
        "msg_processing": "[{i}/{n}] Processing {name}...",
        "msg_profile_start": "📦 Profile: {name} ({n} files)",
        "msg_profiles_need": "❌ --profiles needs at least one preset name",
        "msg_quality": "Quality",
        "msg_quality_label": "Quality",
        "msg_quality_max": "Quality max",
        "msg_quality_range": "Quality range: 5–{q}",
        "msg_rating_label": "rating",
        "msg_rename_in_place": "📝 Renaming in place",
        "msg_rename_note": "(note: in-place rename replaces the original name)",
        "msg_rename_ok": "📊 Success {ok}/{total} files",
        "msg_report_err": "❌ Report write error",
        "msg_report_profiles_conflict": "⚠️  --report and --profiles cannot be used together; skipped report",
        "msg_resize_label": "Resize",
        "msg_same_as_source": "(same as source)",
        "msg_saved": "Saved",
        "msg_scale_label": "Scale",
        "msg_scanning": "🔍 Scanning {n} file(s)...",
        "msg_settings_applied": "Settings that would be applied:",
        "msg_start_processing": "🚀 Processing...",
        "msg_subfolder": "Subfolder",
        "msg_success": "Success",
        "msg_summary": "Summary",
        "msg_synced_date": "✅ DateTimeOriginal ← mtime (reverse sync)",
        "msg_target_format": "Target format",
        "msg_target_size": "Target size",
        "msg_target_size_header": "🎯 Target: ≤ {size}",
        "msg_token_generated": "🔐 Token generated",
        "msg_total_compressed": "Total compressed",
        "msg_total_ok": "Total {total}, OK: {ok}",
        "msg_total_original": "Total original",
        "msg_verb_delete": "delete",
        "msg_verb_move": "move to duplicates folder",
        "msg_verb_report": "report",
        "msg_write_failed": "⚠️  {n} file(s) failed to write",
        "msg_written_csv": "✅ Written from CSV",
        "msg_written_json": "✅ Written from JSON",
        "msg_yes": "Yes",
        "val_format": "Invalid format: '{fmt}'. Choose from: {formats}",
        "val_size": "Invalid size format: '{val}'. Supported: {supported}",
        "help___directory": "Directory to watch",
        "help___files": "Image files or glob patterns",
        "help___image_files": "Image files",
        "help___paths": "Files, directories, or glob patterns",
        "help___plugin_name": "Plugin name",
        "help___plugin_name_alnum": "Plugin name (alnum + -)",
        "help___plugin_name_eg": "Plugin name (e.g. scunet)",
        "help___preset_name": "Preset name",
        "help___preset": "Apply a saved preset as base options (explicit CLI args win)",
        "cmd_batch": "Batch process (compress + convert + resize)",
        "cmd_bench": "Batch pipeline benchmark",
        "cmd_analyze": "Perceptual analysis (histogram/color/exposure - grading feedback loop for agents)",
        "help___sample_size": "Max sample dimension (default 256, smaller is faster)",
        "help___grid": "Regional feedback grid (4/8 - per-cell luma/color + sky/skin/overexposed boxes)",
        "cmd_lr_scan": "Lightroom data bridge report (auto-discover catalogs/XMP, coverage + training export)",
        "help___lr_paths": ".lrcat/.xmp files or dirs; default scans ~/Pictures and ~/Desktop",
        "help___lr_export": "Export training JSONL (per line: path + PhotoS params) to this dir",
        "help___lr_render": "Render before images (rawpy default develop → JPEG) to this dir",
        "help___lr_sanitize": "Sanitize export: basename-only paths (hides local dir structure), mapping in lr_paths.json",
        "cmd_lr_train": "Train auto-tone model (ridge regression, pure numpy, 9 global params)",
        "help___lr_data": "lr_records.jsonl (from lr-scan --export-dir)",
        "help___lr_images": "before image dir (from --render-dir; default uses JSONL image key)",
        "help___lr_out": "Output file (default auto_tone.npz / eval.json)",
        "help___lr_lambda": "Ridge regularization strength (default 1.0)",
        "cmd_lr_predict": "Auto-tone inference: image -> 9 global params",
        "help___lr_image": "Input image path",
        "help___lr_model": "Model file (lr-train ridge or CLIP+MLP npz, default auto_tone.npz)",
        "cmd_lr_recipes": "Edit recipe clustering (personal style library, KMeans in param space)",
        "help___lr_k": "Number of clusters (default 6)",
        "cmd_lr_similar": "Similar-edit retrieval (content-feature kNN -> past edit as starting point)",
        "cmd_lr_eval": "Teacher eval set prep (sample -> before/after render pairs + scoring template)",
        "help___lr_sample": "Sample size (default 200)",
        "cmd_lr_merge": "Merge multi-machine packages (dedupe + before images + provenance)",
        "help___lr_packages": "Package dirs from other machines (containing lr_records.jsonl [+before/])",
        "help___lr_merge_out": "Merged output dir (default merged/)",
        "msg_lr_merged": "Merged {p} packages: {n} records ({e} edited), {img} before images copied -> {out}",
        "msg_lr_rendered": "Rendered before images: {n} (skipped {skip}, failed {failed})",
        "msg_lr_trained": "Model trained: {n} samples, R²={r2}, saved to {out}",
        "msg_lr_recipes": "Recipe clusters: {k} / {n} samples",
        "msg_lr_similar": "Similar edits ({img}) -> top {k}",
        "msg_lr_eval": "Eval set: {n} pairs (before/after), saved to {out}",
        "cmd_diff": "Compare versions (PSNR/SSIM/mean-abs-diff - numeric before/after)",
        "help___diff_paths": "Two image paths (before after)",
        "cmd_audit": "Quality gate (pass/fail + reasons - agent stop condition)",
        "cmd_suggest": "Rule-based parameter suggestions (analyze → params, zero models)",
        "help___scale": "Scale suggestion magnitude 0-1 (gentle=0.5)",
        "suggest_neutral": "metrics neutral - nothing objectively wrong",

        "help___over_max": "Max overexposed pixels (percent, default 2.0)",
        "help___under_max": "Max underexposed pixels (percent, default 2.0)",
        "help___blur_min": "Min blur score (default 0.05)",
        "help___aesthetic": "Aesthetic gate: min score 1-10 (needs the auto-tone plugin: SigLIP head or qwen extra)",
        "cmd_preview": "Visual snapshot (downscaled JPEG base64 + histogram PNG - eyes for multimodal agents)",
        "help___max_dim": "Max long-edge dimension (default 1024)",
        "help___no_histogram": "Skip histogram PNG output",
        "help___trace": "Trace log dir (one line per file: before-analyze -> params -> after-analyze, training data format)",
        "help___write_xmp": "Write an .xmp sidecar per input (Lightroom opens the original and continues editing; with --auto-tone the real predicted params are exported)",
        "cmd_xmp_export": "Write Lightroom XMP sidecars (adjustments + rating/keywords, editable in LR)",
        "help_xmp_preset": "Take adjustment values from a preset",
        "help_xmp_options": "Adjustment fields as a JSON object (e.g. '{\"ev\":0.3}')",
        "help_xmp_rating": "Write xmp:Rating 0-5",
        "help_xmp_keywords": "Comma-separated keywords (dc:subject, visible in the LR keyword panel)",
        "help_xmp_title": "Image title (dc:title)",
        "help_xmp_out_dir": "Sidecar output directory (default: next to the original)",
        "msg_xmp_no_source": "No adjustment source or metadata given (pick one of --preset/--options/--auto-tone/--rating/--keywords/--title)",
        "cmd_autopilot": "Unattended pipeline (watch -> suggest/auto-tone -> audit -> route to passed/review)",
        "help_autopilot_dir": "Directory to watch",
        "help_autopilot_out_dir": "Output root (default <watch>/photo-s-out/ with passed/ review/ and autopilot.jsonl)",
        "help_autopilot_mode": "Param source: suggest (zero-model rules) / auto_tone (needs plugin) / both (fix bias, then style)",
        "help_autopilot_scan_existing": "Process images already in the dir at startup (default: new arrivals only)",
        "help_autopilot_log": "JSONL trace path (default <out-root>/autopilot.jsonl)",
        "msg_autopilot_start": "🚁 Autopilot watching {dir} (mode={mode}) -> {out}",
        "msg_autopilot_stopped": "🚁 Stopped.",
        "cmd_index": "Build a semantic index (embed plugin = SigLIP text+image; fallback = built-in 84-dim, image-only)",
        "help_index_index": "Index file path (default <common-root>/.photo-s-index.npz)",
        "help_index_rebuild": "Ignore the old index and rebuild fully",
        "help_index_extractor": "Force an extractor name (default: auto, embed plugin preferred)",
        "help_index_tags": "Auto-tag after indexing: comma-separated tags (needs a text encoder; writes EXIF keywords)",
        "help_index_min_score": "Cosine floor for keeping a tag (default 0.2)",
        "help_index_max_tags": "Max tags per image (default 5)",
        "help_index_write_xmp": "Also write tags as an XMP dc:subject sidecar (visible in the LR keyword panel)",
        "msg_index_done": "Indexed {total} images (new {indexed} / kept {kept} / removed {removed}, {extractor}) -> {index}",
        "msg_index_tags": "Auto-tagged {n} images",
        "cmd_find": "Semantic search: text query or search-by-image over an index",
        "help_find_query": "Natural-language query (English works best - SigLIP is English-centric)",
        "help_find_image": "Search by image: reference image path",
        "help_find_index": "Index file path (default ./.photo-s-index.npz)",
        "help_find_k": "Return top K (default 10)",
        "help_find_min_score": "Filter floor on the cosine score",
        "msg_find_no_query": "missing query: give text or --image reference",
        "msg_lr_header": "Lightroom coverage report ({sec}s)",
        "msg_lr_catalogs": "catalog     photos edited AI-mask point-color",
        "msg_lr_summary": "Total {photos} photos + {xmp} XMP, {edited} edited",
        "msg_lr_params": "-- Global parameter usage (top 15) --",
        "msg_lr_masks": "-- Mask kind distribution --",
        "msg_lr_tools": "-- Tool usage history (top 12) --",
        "msg_lr_unmapped": "-- Unmapped gaps --",
        "msg_lr_export": "Training data exported: {path}",
        "cmd_blurfaces": "Detect and blur/pixelate faces (privacy)",
        "cmd_check": "Verify image integrity",
        "cmd_commands": "Commands",
        "cmd_compress": "Compress image file size",
        "cmd_config": "Manage config file (photo-s.toml)",
        "cmd_contact_sheet": "Contact sheet (grid montage)",
        "cmd_convert": "Convert image format",
        "cmd_cull": "Cull by exposure & sharpness",
        "cmd_dedup": "Find duplicate images",
        "cmd_delete": "Delete preset",
        "cmd_exif": "Read, write & filter EXIF metadata",
        "cmd_fetch": "Pre-download model weights",
        "cmd_gallery": "Generate HTML gallery",
        "cmd_hash": "Checksum manifest (SHA-256)",
        "cmd_hdr": "Merge bracketed exposures into HDR (exposure fusion)",
        "cmd_info": "Show supported formats",
        "cmd_init": "Create a default config file",
        "cmd_install": "Install an official plugin",
        "cmd_list": "List installed & available",
        "cmd_load": "Print preset config",
        "cmd_lens_profile": "Manage lens profiles (save/list/delete)",
        "cmd_mcp": "Start MCP server (stdio)",
        "cmd_plugin": "Manage official plugins",
        "cmd_preset": "Manage presets",
        "cmd_rename": "Batch rename images",
        "cmd_save": "Save a preset",
        "cmd_scaffold": "Scaffold a new plugin",
        "cmd_select": "Sort keepers/rejects by rating",
        "cmd_serve": "Start REST API server",
        "cmd_show": "Show effective config",
        "cmd_uninstall": "Uninstall a plugin",
        "cmd_watch": "Watch folder and auto-process",
        "desc": "PhotoS — Batch Image Compression & Format Conversion",
        "epilog": "Usage Examples:\n  photo-s compress *.jpg -q 80                 Batch-compress JPEG images\n  photo-s compress *.jpg --target-size 500KB   Auto-tune quality under 500KB\n  photo-s compress *.ARW -q 90                 Convert RAW photos to JPEG\n  photo-s compress *.ARW --raw-half-size -q 85 Fast RAW half-size processing\n  photo-s convert *.png -f webp -q 85          Convert PNG to WebP\n  photo-s batch ~/Pictures/ -r -f JPEG -q 70   Recurse a whole directory\n  photo-s batch ~/Pictures/ -r --target-size 2MB  Auto-tune under 2MB\n  photo-s batch . --resize 1920x1080           Batch-resize images\n  photo-s batch . --scale 50                   Scale down to 50%\n  photo-s batch . --no-exif                    Strip EXIF\n  photo-s batch . --dry-run                    Preview (no processing)\n  photo-s batch . --organize date              Subfolders by date\n  photo-s batch . --organize date-camera       Subfolders by date + camera",
        "help___action": "Action: report=report only, move=move to _duplicates, delete=delete, keep-sharpest=keep sharpest (default: report)",
        "help___aperture": "Aperture f-number, e.g. '2.8' or 'f/2.8'",
        "help___artist": "Artist / Photographer",
        "help___auto_exposure": "Auto-exposure target luminance",
        "help___auto_levels": "Auto levels (2%% clip histogram stretch)",
        "help___auto_straighten": "Auto-level the horizon (needs optional opencv)",
        "help___bg": "Background color, e.g. #1a1a1a",
        "help___blur_faces": "Detect & blur faces: blur=Gaussian, pixelate=mosaic (needs photo-s-tools[enhance])",
        "help___blur_faces_margin": "Face-box expansion percent (default 20)",
        "help___cutout": "Cutout / background removal: subject|person|object:class|color:R,G,B[,tol=30][,feather=0][,invert] (needs PNG/WebP/TIFF/AVIF/HEIC output; JPEG errors per file)",
        "help___blur_score": "Compute blur heuristic for inputs",
        "help___brightness": "Brightness multiplier (1.0 = unchanged)",
        "help___camera": "Camera model substring (with --show)",
        "help___caption": "Caption (= description)",
        "help___clarity": "Clarity: large-radius local contrast (LR clarity)",
        "help___color_grading": "3-way color grading: per-zone “zone:hue,sat” (shadows/midtones/highlights)",
        "help___cols": "Columns per row (default: 4)",
        "help___config": "Config file path (photo-s.toml)",
        "help___contrast": "Contrast multiplier (1.0 = unchanged)",
        "help___copyright": "Copyright",
        "help___crop": "Crop, e.g. 800x600+100+50 (offsets optional → centered)",
        "help___curves": "Point curves: per-channel control points “ch:x,y;x,y|ch:...” (PCHIP monotone)",
        "help___crop_ratio": "Center-crop to aspect ratio, e.g. 16:9",
        "help___date": "Date taken, e.g. '2024:07:30 14:30:00'",
        "help___dehaze": "Dehaze via dark-channel prior (negative adds haze)",
        "help___date_from": "Filter: start date (with --show)",
        "help___date_from_mtime": "Set DateTimeOriginal from the file's mtime (reverse sync)",
        "help___date_shift": "Date shift, e.g. \"-5h30m\" / \"+2h\" / \"1d\"",
        "help___date_to": "Filter: end date (with --show)",
        "help___auto_tone": "AI auto-tone strength 0-1 (needs the auto-tone plugin; zero-model alternative: photo-s suggest)",
        "help___denoise": "NLM denoise strength 3-20 (needs optional opencv)",
        "help___desc": "Description",
        "help___description": "Image description",
        "help___dir": "Output dir (default plugins/<name>)",
        "help___dry_run": "Dry run — preview only",
        "help___ev": "EV compensation in stops, e.g. -1.5 / +1",
        "help___evaluate": "Compute SSIM quality score (input vs output)",
        "help___export_sharpen": "Export sharpening 0-2 (LR-style output-stage USM, radius scales with output resolution; 0 off)",
        "help___flatten_cmyk": "Convert CMYK input to RGB",
        "help___highlight_recovery": "Highlight recovery 0-1 (LR-style: compress flat clipped highlights back to visible gradient; 0 off)",
        "help___flip": "Mirror: h horizontal / v vertical",
        "help___focal": "Focal length in mm, e.g. '50'",
        "help___format": "Target format (default: JPEG, case-insensitive)",
        "help___from_csv": "Batch write from CSV (columns: path,rating,keywords,caption,title,...)",
        "help___from_json": "Batch write from JSON ([{path, rating, keywords, ...}, ...])",
        "help___gamma": "Gamma (1.0 = unchanged, display brightness)",
        "help___gps": "GPS coords 'lat,lon' (writes the same position to every file, e.g. 31.23,121.47)",
        "help___gpx_trace": "Geo-tag from a GPX track (matches EXIF datetime)",
        "help___grain": "Film grain: luminance-weighted noise “amount[,size]”",
        "help___masks": "Local masks: “name:type:params;...” type=linear (x0,y0,x1,y1)/radial (cx,cy,rx,ry)/color (r,g,b,tol=)/subject/person/object:class/brush:x,y,r|x,y,r (-x,y,r subtracts)/combo:A&B|A-B, relative 0-1; optional trailing ,feather=/,invert (e.g. “sky:linear:0.5,0,0.5,1,feather=0.3”)",
        "help___mask_adjust": "Per-mask local adjustments: “name:key=value,...;...” key=exposure/brightness/contrast/saturation/vibrance/clarity/texture/sharpen/temp/tint/blur or string keys curves/hsl/color_grading/vignette/grain (curly-brace-wrapped, same format as global)",
        "help___lens_distort": "Lens distortion: radial k1, positive fixes barrel (pulls edges out), negative pincushion",
        "help___lens_vignette": "Lens vignette removal: “amount[,midpoint]” lifts the corners",
        "help___lens_ca": "Chromatic aberration: “r_scale,b_scale” per-channel radial scales (e.g. “0.999,1.001”)",
        "help___lens_profile": "Apply a named lens profile (maintained via lens-profile save; explicit lens_* args win)",
        "help___lens_profile_name": "Lens profile name (e.g. “RF 24-70mm f/2.8”)",
        "help___grayscale": "Convert to grayscale",
        "help___hdr_align": "Align handheld brackets (AlignMTB, kills ghosting)",
        "help___host": "Listen address (default: 127.0.0.1)",
        "help___hsl": "HSL split: per-color shifts “color:h,s,l;...” (8 domains)",
        "help___point_color": "Point color: sampled-color targeting “r,g,b:hue,sat,lum[,range];...”, mask centred on the sample",
        "help___images": "Limit to first N images",
        "help___iso": "ISO speed, e.g. 400",
        "help___jobs": "Parallel worker threads (default: auto, i.e. min(CPUs, 8))",
        "help___jpeg_subsampling": "JPEG chroma subsampling 444/422/420 (default 420; 444 keeps full color, larger files)",
        "help___json": "Output JSON format for AI agents",
        "help___keep_min": "Keeper threshold: rating ≥ N moves to the selects dir (default 4)",
        "help___keep_mtime": "Preserve source modification time",
        "help___keywords": "Keywords, comma-separated (write: set; --show: any-match filter)",
        "help___language": "UI language (default auto, follows the system)",
        "help___lens": "Lens model, e.g. 'FE 24-70mm F2.8 GM'",
        "help___levels": "Manual levels: “black,white[,gamma]”",
                "help___cull_score": "quality-score mode (0-100 weighted ranking, no rejection)",
        "help___cull_burst": "burst grouping: cluster by EXIF time, keep the best score per group (pair with --score)",
        "help___cull_gap": "burst gap in seconds (default 2.0)",
        "msg_cull_burst_best": "burst keepers",
"help___list": "Output matching paths only (for piping)",
        "help___list_tools": "List tools & schemas without starting",
        "help___log_curve": "LOG recovery curve (SLOG3 CLOG3 LOGC3 DLOG VLOG HLG)",
        "help___luminance_max": "Max mean luminance (0-1)",
        "help___luminance_min": "Min mean luminance (0-1)",
        "help___lut": "Apply a .cube LUT (built-in trilinear; photo-s-plugin-lut adds tetrahedral + film presets)",
        "help___make": "Camera make (e.g. SONY)",
        "help___max_pixels": "Max pixels on longest side, e.g. 8000. Only downscales.",
        "help___max_straighten_angle": "Max horizon tilt to correct (default: 10°)",
        "help___model": "Camera model (e.g. ILCE-7M4)",
        "help___no_auto_rotate": "Disable auto-rotate by EXIF",
        "help___no_exif": "Strip EXIF metadata",
        "help___no_optimize": "Disable PIL optimize pass",
        "help___organize": "Organize into subfolders. Presets: date, camera, date-camera. Custom: {year}/{month}/{camera} etc.",
        "help___output": "Output image path (.jpg/.png)",
        "help___output_dir": "Output directory (default: same as source)",
        "help___overexposed_max": "Max overexposed %% (default: no filter)",
        "help___overwrite": "Overwrite existing files",
        "help___pad": "Letterbox to aspect ratio, e.g. 16:9",
        "help___pad_bg": "Letterbox background (default: #000000)",
        "help___path": "Output path (default: ./photo-s.toml)",
        "help___pattern": "Rename pattern, vars: {year} {month} {day} {date} {time} {camera} {make} {original} {iso} {focal} {seq}",
        "help___port": "Port (default: 8787)",
        "help___prefix": "Output filename prefix",
        "help___print_size": "Print size, e.g. 8x10@300dpi (center-crop + exact pixels)",
        "help___profiles": "Multi-profile: run each preset as its own output pass (comma-separated preset names, e.g. web,thumb)",
        "help___progressive": "Use progressive JPEG encoding",
        "help___quality": "Output quality 1-100 (default: 85)",
        "help___rating": "Rating 0-5 (write: set; --show: exact filter)",
        "help___rating_min": "Minimum rating (with --show)",
        "help___raw_half_size": "RAW half-size decode (faster)",
        "help___raw_no_auto_bright": "Disable RAW auto brightness",
        "help___raw_demosaic": "RAW demosaic algorithm auto/ahd/vng/ppg/dcb/dht/amaze (default auto; amaze is highest quality, slowest)",
        "help___raw_color_space": "RAW output color space sRGB/AdobeRGB/ProPhotoRGB (default sRGB, auto-ICC-tagged; wider gamuts are untagged — tag in your editor)",
        "help___raw_16bit": "16-bit RAW decode; TIFF output written at 16-bit (needs tifffile; only meaningful for pure conversion, JPEG stays 8-bit)",
        "help___ready_file": "Write a handshake JSON {port, token, pid} for host agents",
        "help___recursive": "Recursively search directories",
        "help___reject_max": "Reject threshold: rating ≤ N moves to the rejects dir (default 2)",
        "help___remove_original": "Delete original after processing",
        "help___rename": "Smart rename, vars: {year} {month} {day} {date} {time} {camera} {make} {original} {iso} {focal} {seq}",
        "help___report": "Write per-file CSV report",
        "help___resize": "Resize dimensions, e.g. 1920x1080, 800x, x600",
        "help___resume": "Skip files whose output already exists (resume)",
        "help___rotate": "Rotate degrees (positive = clockwise)",
        "help___rotate_bg": "Rotation corner fill color (default: black)",
        "help___saturation": "Saturation multiplier (1.0 = unchanged)",
        "help___scale": "Scale percentage, e.g. 50",
        "help___scrub": "Strip ALL metadata (EXIF+ICC+comment)",
        "help___select_copy": "Copy instead of move (keep originals)",
        "help___selects_dir": "Selects dir: high-rated photos move here",
        "help___rejects_dir": "Rejects dir: low-rated photos move here",
        "help___sepia": "Apply sepia toning",
        "help___sha256": "Use SHA-256 (default)",
        "help___sharpen": "Sharpen (1.0 = unchanged)",
        "help___sharpness_min": "Min blur-score",
        "help___show": "Read mode (filters apply)",
        "help___shutter": "Shutter speed, e.g. '1/250' or '2' (seconds)",
        "help___sizes": "Multi-size, e.g. thumb:480x,screen:1920x1080",
        "help___software": "Software tag",
        "help___srgb": "Tag output with sRGB ICC profile",
        "help___strip_gps": "Strip GPS location data",
        "help___suffix": "Output filename suffix (default: _compressed)",
        "help___sync_date": "Set output mtime from EXIF datetime",
        "help___target_size": "Target file size, e.g. 500KB, 2MB. Auto-tune quality to fit.",
        "help___texture": "Texture: small-radius local contrast (LR texture)",
        "help___threshold": "Hamming distance threshold (default: 5, lower = stricter)",
        "help___thumb": "Thumbnail size (default: 240x240)",
        "help___title": "Title",
        "help___token": "Bearer auth. 'auto' generates a random token",
        "help___underexposed_max": "Max underexposed %%",
        "help___verify": "Verify mode",
        "help___version": "Show version",
        "help___vibrance": "Vibrance: natural saturation, inverse-weighted [-1,1]",
        "help___vignette": "Vignette: radial “amount[,midpoint[,feather]]”",
        "help___watermark_image": "Image watermark path",
        "help___watermark_opacity": "Watermark opacity 0-100",
        "help___watermark_pos": "Watermark position",
        "help___watermark_text": "Text watermark",
        "help___wb": "White balance in Kelvin, e.g. 5600",
        "help___wb_from": "White balance from a reference image",
        "help___wb_tint": "White-balance tint: green(-)/magenta(+) G-M axis [-100,100]",
        "help___yes": "Skip all confirmation prompts",
    },
}


def _t(key: str, lang: Optional[str] = None, **kwargs) -> str:
    """Look up a CLI string in the current (or given) language."""
    l = lang or CURRENT_LANG
    text = STRINGS.get(l, {}).get(key) or STRINGS.get(DEFAULT_LANG, {}).get(key) or key
    return text.format(**kwargs) if kwargs else text


# ── Shared text helpers used by the CLI table authoring ────────────────────

# The original CLI strings were authored as inline "中文 English" pairs. The
# table is split by hand at the language boundary; the regexes below are only
# used by tests to assert "en has no CJK / zh has CJK" purity.
_CJK_RE = re.compile(r"[㐀-鿿　-〿＀-￯]")
