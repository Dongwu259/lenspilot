# PhotoS - Batch Image Compression & Format Conversion Tool

__version__ = "2.5.0"

# 有界放宽 PIL 解压炸弹阈值。
# 默认 89.5MP（89478485）对合法大图（全景拼接、高像素相机，如 112MP）会误报
# DecompressionBombWarning；Pillow 规则：> MAX_IMAGE_PIXELS 发警告，> 2× 抛错。
# 设为 512MP：警告阈值 >512MP，硬报错阈值 >1024MP——仍能拦截真正恶意的解压炸弹，
# 只是把误报带宽放大到合法工作流。在包入口设置，任何子模块（engine/gui 等）导入
# 前即生效；只设一次、绝不运行中切换，天然线程安全（GUI 多线程解码）。
MAX_IMAGE_PIXELS = 512_000_000
from PIL import Image as _Image
_Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
