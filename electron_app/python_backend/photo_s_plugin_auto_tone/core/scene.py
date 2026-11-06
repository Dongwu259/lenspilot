"""场景自适应调色

基于 LR catalog 真实数据统计的场景偏置（数据驱动 vs. 手工）：
- portrait (380 张): 默认（与全局一致）
- bw (49 张): clarity +0.054
- soft_haze (18 张): contrast -0.34, clarity -0.16
- bw_high_contrast (14 张): clarity -0.17
- soft (10 张): exposure -0.034, texture +0.042
- bw_landscape (18 张): exposure +0.18
- auto (51 张): exposure -0.035

偏置表打包在包内 data/scene_biases.json（552 张 LR 分类样本统计）。

用法：
    result = auto_tone_with_scene("photo.jpg", strength=0.5)
"""
import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from PIL import Image

from .predictor import AutoTonePredictor
from .render import render_options
from .style_biases import FIELD_RANGES


# 打包在包内的数据驱动偏置表（可用环境变量指向自定义文件）
DEFAULT_SCENE_BIASES = Path(__file__).resolve().parent.parent / "data" / "scene_biases.json"

# 字段缩放（与 style_biases.apply_style_bias 一致的语义）
SCALE = {'exposure': 1.0, 'contrast': 0.5, 'saturation': 0.5, 'vibrance': 1.0,
         'wb_temp': 0.3, 'wb_tint': 0.3, 'clarity': 1.0, 'texture': 1.0, 'dehaze': 1.0}


# 场景预设名 → scene key（scene_biases.json 内 preset_to_scene 缺失时的兜底）
PRESET_TO_SCENE = {
    # 人像
    '魅力人像': 'portrait',
    '精美人像': 'portrait',
    '增强人像': 'portrait',
    '增强眼部效果': 'portrait',
    '加深眉毛': 'portrait',
    '纹理头发': 'portrait',
    # 黑白
    '黑白 平滑': 'bw',
    '黑白 风景': 'bw_landscape',
    '黑白 高对比度': 'bw_high_contrast',
    '黑白 柔和': 'bw',
    # 风景/天空
    '暴风云': 'landscape_dramatic',
    # 冷暖风格
    '暖色流行': 'warm',
    '冷色阴影和暖色高光': 'split_tone',
    # 其他
    '流行': 'auto',
    '柔和': 'soft',
    '柔冷色': 'cool',
    '蓝色戏剧': 'cool_dramatic',
    '柔和薄雾': 'soft_haze',
}


class SceneClassifier:
    """场景分类器（基于 LR preset names 的数据驱动偏置）

    用法：
        classifier = SceneClassifier()
        bias = classifier.get_bias("portrait")
        opts = classifier.apply_bias(base_options, "portrait", strength=0.5)
    """

    def __init__(self, scene_biases_path: Optional[str] = None):
        if scene_biases_path is None:
            scene_biases_path = os.environ.get(
                "PHOTOS_AUTO_TONE_SCENE_BIASES", str(DEFAULT_SCENE_BIASES))
        if not os.path.exists(scene_biases_path):
            raise FileNotFoundError(
                f"scene_biases.json not found: {scene_biases_path} "
                "(pip install photo-s-plugin-auto-tone>=2.1.0 或设置 "
                "PHOTOS_AUTO_TONE_SCENE_BIASES)")
        with open(scene_biases_path, encoding='utf-8') as f:
            data = json.load(f)
        self.scene_biases = data['scene_biases']
        self.preset_to_scene = data.get('preset_to_scene', PRESET_TO_SCENE)
        self.global_means = data.get('global_means', {})

    def extract_preset_names(self, record: Dict) -> List[str]:
        """从 LR record 提取 preset names（lr-scan 产出的 history 列表）"""
        names = []
        for h in record.get('history', []):
            name = h.get('name', '')
            if '预设' in name and ':' in name:
                pname = name.split(':', 1)[1].strip()
                if pname and pname != '预设数量':
                    names.append(pname)
        return names

    def classify_by_preset(self, preset_names: List[str]) -> str:
        """从 preset names 推断场景"""
        for p in preset_names:
            scene = self.preset_to_scene.get(p)
            if scene and scene != 'unknown':
                return scene
        return 'default'

    def get_bias(self, scene: str) -> Dict[str, float]:
        """获取场景偏置"""
        return self.scene_biases.get(scene, {})

    def apply_bias(self, base_options: Dict[str, float], scene: str,
                   strength: float = 0.5) -> Dict[str, float]:
        """应用场景偏置

        Args:
            base_options: 基础 9 字段预测
            scene: 场景 key
            strength: 偏置强度 0-1（推荐 0.3-0.5）

        Returns:
            修改后的 9 字段 dict
        """
        bias = self.get_bias(scene)
        new_options = dict(base_options)
        for f, b in bias.items():
            if f in new_options:
                lo, hi = FIELD_RANGES[f]
                delta = b * strength * SCALE[f] * (hi - lo) / 2
                new_options[f] = max(lo, min(hi, new_options[f] + delta))
        return new_options


# 便捷入口
def auto_tone_with_scene(
    image_path: str,
    scene: Optional[str] = None,
    strength: float = 0.5,
    render: bool = True,
    output_path: Optional[str] = None,
    predictor: Optional[AutoTonePredictor] = None,
) -> Dict:
    """场景自适应自动调色

    Args:
        image_path: 图像路径
        scene: 场景 key (None = 'default'，自动检测待集成 SigLIP)
        strength: 偏置强度 0-1（推荐 0.3）
        render: 是否渲染输出
        output_path: 自定义输出路径
        predictor: 注入依赖（可选）

    Returns:
        dict {
            'schema_version': 1,
            'options': 9 字段 dict,
            'scene': 场景 key,
            'scene_bias': 应用的偏置,
            'rendered_path': 输出路径,
            'metadata': {...},
        }
    """
    t0 = time.time()

    predictor = predictor or AutoTonePredictor()
    predictor.load()

    img = Image.open(image_path).convert('RGB')

    # 1. base 预测
    base_options = predictor.predict(img)

    # 2. 场景分类
    classifier = SceneClassifier()
    if scene is None:
        # 自动检测（基于 preset names 启发式 or SigLIP）
        # 当前 fallback 到 'default'（待集成 SigLIP）
        scene = 'default'

    # 3. 应用场景偏置
    final_options = classifier.apply_bias(base_options, scene, strength=strength)
    scene_bias = classifier.get_bias(scene)

    # 4. 渲染
    rendered_path = None
    if render:
        if output_path is None:
            stem = Path(image_path).stem
            output_path = str(Path(image_path).parent / f'{stem}_scene_{scene}.jpg')
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        render_options(img, final_options, output_path=output_path)
        rendered_path = output_path

    return {
        'schema_version': 1,
        'options': final_options,
        'scene': scene,
        'scene_bias': scene_bias,
        'rendered_path': rendered_path,
        'metadata': {
            'strength': strength,
            'duration_ms': int((time.time() - t0) * 1000),
        },
    }
