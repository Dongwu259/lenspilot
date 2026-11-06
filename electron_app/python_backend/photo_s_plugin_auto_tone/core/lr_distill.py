# -*- coding: utf-8 -*-
"""lr-v1 蒸馏学生（Qwen3-VL-4B-Instruct + LoRA）—— 用户 LR 修图风格 advisor。

学生由 AI_LR_trainning 蒸馏线训练（服务器 docs/HANDOFF_DISTILL.md）：输入 =
照片 + P67 Inspector 诊断文字，输出 = 单个 JSON（scene / reasoning / 9 参数
语义档位 params / summary），档位契约 = docs/agent-lr-grade-schema.md v1.0
（FROZEN）。300 张 bench：JSON 100% 合规、exposure 分场景均值与 V24e 重合。

资产（绝不隐式下载 GB 级权重——设置页红线）：
    PHOTOS_AUTO_TONE_LR_V1_BASE   基座快照目录（transformers 格式 4B）
    PHOTOS_AUTO_TONE_LR_V1_LORA   adapter 目录（adapter_config.json +
                                   adapter_model.safetensors）
    缺省探测仓库根 distill_assets/（Qwen3-VL-4B-Instruct/ 与 lr-v1/，
    开发机零配置）；探测不到 → ready()=False，上层降级 auto→suggest。

口径换算（档位 → 引擎终值；输出不再经过 pipeline._convert_wb_to_engine，
那个适配层是给「LR 绝对 K 预测」的 MLP 用的，学生是偏移档口径）：
    exposure      档心 k/3 EV（E0 不输出 = 不动）
    contrast/saturation/...  schema §3 代表值原值（C0 众数 1.06 是提取
                  管线系统性偏移的忠实映射，照用）
    wb_temp       偏移档 → 6500+offset（引擎中性 6500K，bench 渲染同口径；
                  WB_N 不输出 = 保持相机白平衡）
    wb_tint       偏移档 → 偏移值原值（TN 不输出）

wb 档偏移步长以 schema §4 FROZEN 表为准（-300/-700/-1200K；tint ±5/12/20）
——bench_eval_student.BAND_VALUE 里的 -400/-800 与 tint ±7/14/21 与 schema
不一致，属 bench 脚本笔误；因数据 wb 几乎全中性（bench 100% 命中均为中性
档），该偏差从未生效，此处不沿用。
"""
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from PIL import Image

from . import lr_inspector

# ── 学生契约（sft_windows.jsonl 首条 system，601 字符，逐字）────────────────
# sha256 = b83c35908345d86bc3494a224f1615ad5a115b9d1850e3dfeacf983a8cf5f056
# （服务器侧训练数据实测；冒烟测试钉死校验，改动即报错）
SYSTEM_CONTRACT = (
    "你是资深摄影师风格的修图参数顾问。给定照片与其结构化诊断(Inspector)，输出调色决策：先讲场景判读与理由(reasoning, 2-4条, 引用证据)，再给9参数语义档位(params)，最后一句总结(summary, ≤30字)。输出必须是单个JSON对象，档位记号只能用下表：\n"
    "exposure: E{k}, k∈[-6,+8]整数, 档心k/3EV；contrast: C-2(0.70)/C-1(0.92)/C0(1.06)/C+1(1.13)；saturation: S-2(0.76)/S-1(0.95)/S0(1.00)/S+1(1.10)；vibrance: V-2(-0.20)/V-1(-0.05)/V0(0)/V+1(0.07)/V+2(0.15)/V+3(0.22)；clarity: CL-2(-0.25)/CL-1(-0.10)/CL0(0)/CL+1(0.10)/CL+2(0.30)；texture: TX-2(-0.25)/TX-1(-0.12)/TX0(0)/TX+1(0.15)；dehaze: DH0(0)/DH+1(0.08)/DH+2(0.39)；wb_temp: WB_N(0)或WB_C1..3/WB_W1..3；wb_tint: TN或TG1..3/TM1..3。params 每字段双写 {\"band\": 记号, \"value\": 档心值}。"
)

USER_TEMPLATE = (
    "Inspector 诊断：\n{inspector}\n\n"
    "请基于照片与上述诊断，给出你的调色决策（场景判读→理由→9参数档位→总结）。"
)

FIELDS = ("exposure", "contrast", "saturation", "vibrance",
          "wb_temp", "wb_tint", "clarity", "texture", "dehaze")

_E_RE = re.compile(r"^E([+-]?\d+)$")
BAND_RE = {
    'contrast': re.compile(r'^C(-2|-1|0|\+1)$'), 'saturation': re.compile(r'^S(-2|-1|0|\+1)$'),
    'vibrance': re.compile(r'^V(-2|-1|0|\+1|\+2|\+3)$'),
    'clarity': re.compile(r'^CL(-2|-1|0|\+1|\+2)$'),
    'texture': re.compile(r'^TX(-2|-1|0|\+1)$'),
    'dehaze': re.compile(r'^DH(0|\+1|\+2)$'),
    'wb_temp': re.compile(r'^WB_(N|[CW][123])$'),
    'wb_tint': re.compile(r'^(TN|T[GM][123])$'),
}

# schema §3 代表值（FROZEN）
BAND_VALUE = {
    'contrast': {'C-2': 0.70, 'C-1': 0.92, 'C0': 1.06, 'C+1': 1.13},
    'saturation': {'S-2': 0.76, 'S-1': 0.95, 'S0': 1.00, 'S+1': 1.10},
    'vibrance': {'V-2': -0.20, 'V-1': -0.05, 'V0': 0.0, 'V+1': 0.07, 'V+2': 0.15, 'V+3': 0.22},
    'clarity': {'CL-2': -0.25, 'CL-1': -0.10, 'CL0': 0.0, 'CL+1': 0.10, 'CL+2': 0.30},
    'texture': {'TX-2': -0.25, 'TX-1': -0.12, 'TX0': 0.0, 'TX+1': 0.15},
    'dehaze': {'DH0': 0.0, 'DH+1': 0.08, 'DH+2': 0.39},
}
# schema §4 偏移档（FROZEN）
WB_TEMP_OFFSET = {'WB_N': 0, 'WB_C1': -300, 'WB_C2': -700, 'WB_C3': -1200,
                  'WB_W1': 300, 'WB_W2': 700, 'WB_W3': 1200}
WB_TINT_OFFSET = {'TN': 0, 'TG1': -5, 'TG2': -12, 'TG3': -20,
                  'TM1': 5, 'TM2': 12, 'TM3': 20}
_WB_ENGINE_NEUTRAL_K = 6500.0   # 引擎 apply_white_balance 中性点（bench 同口径）

# strength 插值用中性值（wb_temp 例外：不做插值，见 _apply_strength）
_NEUTRAL = {"exposure": 0.0, "contrast": 1.0, "saturation": 1.0, "vibrance": 0.0,
            "clarity": 0.0, "texture": 0.0, "dehaze": 0.0, "wb_tint": 0.0}


def _dev_assets_root() -> Path:
    """仓库根 distill_assets/（electron_app/python_backend/插件/core 往上 4 级）。"""
    try:
        return Path(__file__).resolve().parents[4] / "distill_assets"
    except IndexError:  # 路径层级异常（理论不可达）
        return Path.cwd() / "distill_assets"


def parse_json_loose(s: str) -> Optional[dict]:
    """学生输出解析（容忍 ```json 围栏与前后杂文）——bench 同款。"""
    s = (s or "").strip()
    if '```' in s:
        s = re.sub(r'^```[a-z]*\n?|\n?```$', '', s, flags=re.M)
    i, j = s.find('{'), s.rfind('}')
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(s[i:j + 1])
    except json.JSONDecodeError:
        return None


def band_valid(field: str, band: str) -> bool:
    if field == 'exposure':
        return bool(_E_RE.match(band or ''))
    return bool(BAND_RE.get(field) and BAND_RE[field].match(band or ''))


def bands_to_options(bands: Dict[str, str]) -> Dict[str, float]:
    """9 档位 → 引擎口径 options。中性档（E0/WB_N/TN）不输出 = 不动该字段。"""
    opts: Dict[str, float] = {}
    m = _E_RE.match(bands.get('exposure') or '')
    if not m:
        raise ValueError(f"bad exposure band: {bands.get('exposure')!r}")
    k = int(m.group(1))
    if k != 0:
        opts['exposure'] = k / 3.0
    for f in ('contrast', 'saturation', 'vibrance', 'clarity', 'texture', 'dehaze'):
        band = bands.get(f) or ''
        if band not in BAND_VALUE[f]:
            raise ValueError(f"bad {f} band: {band!r}")
        opts[f] = float(BAND_VALUE[f][band])
    t_band = bands.get('wb_temp') or ''
    if t_band not in WB_TEMP_OFFSET:
        raise ValueError(f"bad wb_temp band: {t_band!r}")
    if WB_TEMP_OFFSET[t_band]:
        opts['wb_temp'] = _WB_ENGINE_NEUTRAL_K + WB_TEMP_OFFSET[t_band]
    i_band = bands.get('wb_tint') or ''
    if i_band not in WB_TINT_OFFSET:
        raise ValueError(f"bad wb_tint band: {i_band!r}")
    if WB_TINT_OFFSET[i_band]:
        opts['wb_tint'] = float(WB_TINT_OFFSET[i_band])
    return opts


def _apply_strength(options: Dict[str, float], strength: float) -> Dict[str, float]:
    """strength<1 时向中性值插值（与 render.apply_strength 同思路，但 WB 例外：
    温度插值没有安全锚点，直接放弃 WB = 保守不动白平衡）。"""
    if strength >= 0.999:
        return options
    out: Dict[str, float] = {}
    for k, v in options.items():
        if k == 'wb_temp':
            continue
        n = _NEUTRAL.get(k)
        out[k] = float(v) if n is None else float(n) + (float(v) - n) * float(strength)
    return out


# ── 防御式解析（借鉴 lumen-raw nl_edit.plan 的四件套）────────────────────
# 学生输出 bench 100% 合规，这层为两件事兜底：① 运行时偶发劣化（截断/围栏
# 漏失）；② 未来语言入口由通用 LLM 产档位（别名/嵌套/字符串数值/越界全都会
# 出现）。原则：模型永远无法产生非法 recipe——非法项中性化 + notes 说明，
# ≥7/9 字段合法才抢救，否则整体判失败交上层降级。

# 字段别名（小写规范化后匹配；中文供未来通用 LLM 使用）
_FIELD_ALIASES = {
    'exposure': ('exposure', 'expose', 'ev', '曝光', '曝光度', '亮度'),
    'contrast': ('contrast', '对比度', '对比'),
    'saturation': ('saturation', '饱和度'),
    'vibrance': ('vibrance', '自然饱和度', '鲜艳度'),
    'wb_temp': ('wb_temp', 'temp', 'temperature', '色温', '白平衡'),
    'wb_tint': ('wb_tint', 'tint', '色调'),
    'clarity': ('clarity', '清晰度'),
    'texture': ('texture', '纹理', '质地'),
    'dehaze': ('dehaze', '去雾', '去朦胧', '除霾'),
}

_NEUTRAL_BAND = {'exposure': 'E0', 'contrast': 'C0', 'saturation': 'S0',
                 'vibrance': 'V0', 'wb_temp': 'WB_N', 'wb_tint': 'TN',
                 'clarity': 'CL0', 'texture': 'TX0', 'dehaze': 'DH0'}

_E_K_RANGE = (-6, 8)   # schema §2：k∈[-6,+8]


def _norm_key(s) -> str:
    return (str(s or '')).strip().strip('\'"').lower().replace(' ', '_').replace('-', '_')


def _norm_band(s) -> str:
    b = str(s or '').strip().strip('\'"').upper()
    return b.replace('＋', '+').replace('－', '-').replace(' ', '')


def _extract_json(text: str) -> Optional[dict]:
    """括号配平扫描（引号/转义感知），失败再试尾逗号修复。比 find/rfind 强：
    尾部杂文里出现 '}' 也不会截错。"""
    s = (text or '').strip()
    if '```' in s:
        s = re.sub(r'^```[a-z]*\n?|\n?```$', '', s, flags=re.M).strip()
    start = s.find('{')
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                cand = s[start:i + 1]
                try:
                    return json.loads(cand)
                except json.JSONDecodeError:
                    # 尾逗号修复（通用 LLM 高频笔误）：{"a":1,} → {"a":1}
                    fixed = re.sub(r',\s*([}\]])', r'\1', cand)
                    try:
                        return json.loads(fixed)
                    except json.JSONDecodeError:
                        return None
    return None


def _as_number(v) -> Optional[float]:
    """字符串/全角符号数值强制转换（lumen _number 的档位版）。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace('＋', '+').replace('－', '-').replace('，', '.')
    m = re.search(r'[-+]?\d*\.?\d+', s)
    return float(m.group(0)) if m else None


def _nearest_band(field: str, value: float) -> Optional[str]:
    """数值 → 最近档（band 缺失时的反推：exposure k=round(v*3)，其余查代表值/偏移表）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if field == 'exposure':
        k = max(_E_K_RANGE[0], min(_E_K_RANGE[1], int(round(v * 3))))
        return f'E+{k}' if k > 0 else (f'E{k}' if k < 0 else 'E0')
    if field in ('wb_temp', 'wb_tint'):
        table = WB_TEMP_OFFSET if field == 'wb_temp' else WB_TINT_OFFSET
        neutral_key = 'WB_N' if field == 'wb_temp' else 'TN'
        items = [(b, o) for b, o in table.items() if o]
        smallest = min(abs(o) for _, o in items)
        if abs(v) < smallest / 2:   # 中性死区：0 附近的值不被吸到最近偏移档
            return neutral_key
        return min(items, key=lambda bo: abs(v - bo[1]))[0]
    table = BAND_VALUE.get(field) or {}
    if not table:
        return None
    return min(table.items(), key=lambda kv: abs(v - kv[1]))[0]


def _band_from_entry(field: str, entry) -> Optional[str]:
    """条目 → 档位。接受 {'band': ...}/纯字符串/{'value': 数值(反推)}。"""
    if isinstance(entry, dict):
        b = _norm_band(entry.get('band') or '')
        if b:
            return b
        return _nearest_band(field, _as_number(entry.get('value')))
    if isinstance(entry, str):
        b = _norm_band(entry)
        return b if b else None
    return _nearest_band(field, _as_number(entry))


def parse_student_output(reply: str):
    """学生/通用 LLM 回复 → (bands 或 None, notes 列表, 原始 dict 或 None)。

    ≥7/9 字段合法 → 非法/缺失字段按中性档补齐（notes 记录）；
    否则返回 (None, notes, obj)——上层走降级链。"""
    notes: list = []
    obj = _extract_json(reply)
    if not isinstance(obj, dict):
        return None, ['无法从输出中提取 JSON 对象'], None
    params = None
    for key in ('params', 'adjustments'):
        if isinstance(obj.get(key), dict):
            params = obj[key]
            break
    if params is None:
        params = obj  # 顶层平铺（漏掉 params 层）
    norm_params = {_norm_key(k): v for k, v in params.items()}

    bands: Dict[str, str] = {}
    n_valid = 0
    for field, aliases in _FIELD_ALIASES.items():
        entry = None
        for a in aliases:
            entry = norm_params.get(a)
            if entry is not None:
                break
        band = _band_from_entry(field, entry) if entry is not None else None
        if field == 'exposure' and band and _E_RE.match(band):
            k = int(_E_RE.match(band).group(1))
            if not (_E_K_RANGE[0] <= k <= _E_K_RANGE[1]):
                k = max(_E_K_RANGE[0], min(_E_K_RANGE[1], k))
                band = f'E+{k}' if k > 0 else (f'E{k}' if k < 0 else 'E0')
                notes.append(f'exposure 越界，已钉回 E{max(_E_K_RANGE[0], min(_E_K_RANGE[1], k))}')
        if band and band_valid(field, band):
            bands[field] = band
            n_valid += 1
        else:
            notes.append(f'{field}: 无法识别 {entry!r}，按中性档 {_NEUTRAL_BAND[field]} 处理')
    if n_valid < 7:
        notes.append(f'合法字段仅 {n_valid}/9，放弃抢救')
        return None, notes, obj
    for field, nb in _NEUTRAL_BAND.items():
        bands.setdefault(field, nb)
    return bands, notes, obj


class LRDistillAdvisor:
    """lr-v1 蒸馏学生加载与推理（advisor.py 同款加载形态）。"""

    def __init__(self, lora_path: Optional[str] = None,
                 base_path: Optional[str] = None):
        self.base_path = (base_path
                          or os.environ.get("PHOTOS_AUTO_TONE_LR_V1_BASE")
                          or self._dev_default_base())
        self.lora_path = (lora_path
                          or os.environ.get("PHOTOS_AUTO_TONE_LR_V1_LORA")
                          or self._dev_default_lora())
        self.model = None
        self.processor = None
        self.error: Optional[str] = None
        self._load_attempted = False

    @staticmethod
    def _dev_default_base():
        p = _dev_assets_root() / "Qwen3-VL-4B-Instruct"
        return str(p) if (p / "config.json").is_file() else "Qwen/Qwen3-VL-4B-Instruct"

    @staticmethod
    def _dev_default_lora():
        p = _dev_assets_root() / "lr-v1"
        return str(p) if (p / "adapter_config.json").is_file() else None

    def ready(self) -> bool:
        """资产是否就绪（只查文件，不加载模型）——上层据此决定是否降级。"""
        if not (self.lora_path
                and os.path.isfile(os.path.join(self.lora_path, "adapter_model.safetensors"))
                and os.path.isfile(os.path.join(self.lora_path, "adapter_config.json"))):
            self.error = (f"lr-v1 LoRA assets not found ({self.lora_path!r}); "
                          "set PHOTOS_AUTO_TONE_LR_V1_LORA or place distill_assets/lr-v1/")
            return False
        if not (os.path.isdir(self.base_path)
                and os.path.isfile(os.path.join(self.base_path, "config.json"))
                and any(Path(self.base_path).glob("model*.safetensors"))):
            self.error = (f"Qwen3-VL-4B base snapshot not found ({self.base_path!r}); "
                          "set PHOTOS_AUTO_TONE_LR_V1_BASE to a local transformers snapshot")
            return False
        return True

    def _device_dtype(self):
        from .. import models
        import torch
        device = os.environ.get("PHOTOS_LR_DISTILL_DEVICE") or models.pick_device()
        if device == "cuda":
            dtype = torch.bfloat16
        else:
            # 4B fp32 ≈ 16GB 内存；32GB 机器可跑，紧的话设 bf16 减半
            dtype = torch.bfloat16 if os.environ.get(
                "PHOTOS_LR_DISTILL_DTYPE", "").lower() == "bf16" else torch.float32
        return device, dtype

    def load(self):
        if self._load_attempted:
            return
        self._load_attempted = True
        if not self.ready():
            return
        try:
            import torch
            from transformers import Qwen3VLForConditionalGeneration, AutoProcessor
            from peft import PeftModel

            device, dtype = self._device_dtype()
            kwargs = {"dtype": dtype}
            if device == "cuda":
                kwargs["device_map"] = "cuda"
            self.model = Qwen3VLForConditionalGeneration.from_pretrained(
                self.base_path, **kwargs)
            self.processor = AutoProcessor.from_pretrained(self.base_path)
            self.model = PeftModel.from_pretrained(
                self.model, self.lora_path, is_trainable=False)
            if device != "cuda":
                self.model = self.model.to(device)
            self.model.eval()
        except Exception as e:  # 加载失败 → 上层降级，不炸编辑器
            self.error = f"load failed: {e}"
            self.model = None
            self.processor = None

    def advise(self, image, name: str = "", strength: float = 1.0) -> Dict[str, Any]:
        """单图 → {'options', 'narrative', 'raw', 'model_version', 'duration_ms'}
        或 {'error'}（上层降级 auto）。"""
        t0 = time.time()
        if isinstance(image, str):
            name = name or os.path.basename(image)
            img = Image.open(image).convert("RGB")
        else:
            img = image.convert("RGB")

        self.load()
        if self.model is None or self.processor is None:
            return {"error": self.error or "model not loaded"}

        # 训练/bench 输入为 ≤2048 量级图；超限时等比缩到 2048 再做诊断+推理
        w, h = img.size
        if max(w, h) > 2048:
            s = 2048.0 / max(w, h)
            img = img.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)

        try:
            import torch

            txt = lr_inspector.inspector_text(img, name=name or "photo")
            device, _ = self._device_dtype()
            messages = [
                {"role": "system", "content": SYSTEM_CONTRACT},
                {"role": "user", "content": [
                    {"type": "image", "image": img},
                    {"type": "text", "text": USER_TEMPLATE.format(inspector=txt)},
                ]},
            ]
            inputs = self.processor.apply_chat_template(
                messages, add_generation_prompt=True, tokenize=True,
                return_dict=True, return_tensors='pt').to(device)
            with torch.no_grad():
                output = self.model.generate(
                    **inputs,
                    max_new_tokens=512,
                    do_sample=False,
                    pad_token_id=self.processor.tokenizer.pad_token_id or 0,
                )
            gen = output[0, inputs['input_ids'].shape[1]:]
            reply = self.processor.tokenizer.decode(gen, skip_special_tokens=True)
        except Exception as e:
            return {"error": f"inference failed: {e}"}

        bands, notes, o = parse_student_output(reply)
        if bands is None:
            return {"error": f"bad output ({'; '.join(notes[:3])}): {reply[:100]!r}"}
        # bands 已全部合法化（非法项中性化），换算不再抛
        options = _apply_strength(bands_to_options(bands), strength)
        o = o if isinstance(o, dict) else {}
        narrative = {
            "scene": str(o.get('scene') or '')[:120],
            "reasoning": [str(r)[:200] for r in (o.get('reasoning') or [])][:5],
            "summary": str(o.get('summary') or '')[:60],
            "bands": bands,
            "notes": notes[:5],
        }
        return {"options": options, "narrative": narrative, "raw": reply,
                "model_version": "lr-v1",
                "duration_ms": int((time.time() - t0) * 1000)}


_lock = threading.Lock()
_advisor: Optional[LRDistillAdvisor] = None


def get_distill_advisor() -> LRDistillAdvisor:
    """进程内单例（4B 模型只载一次；线程安全）。"""
    global _advisor
    with _lock:
        if _advisor is None:
            _advisor = LRDistillAdvisor()
        return _advisor
