// P0-2 曲线数值一致性：前端 photo-edit.js _curveLut 必须与后端
// photo_s.grade._build_curve_lut 输出一致（容差 ≤1/255，源于四舍五入口径）。
//
// 用法: node smoke_test/test_curve_parity.mjs  →  输出各测试曲线的 256 点 LUT JSON
// 由 smoke_test/test_p0_curve_parity.py 调用并比对。
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const src = readFileSync(join(root, 'electron_app', 'src', 'js', 'photo-edit.js'), 'utf8');
const PhotoEdit = new Function(`${src}; return PhotoEdit;`)();

const CASES = [
  [[0, 0], [255, 255]],                                  // 恒等
  [[0, 0], [128, 180], [255, 255]],                      // 提亮中间调
  [[0, 20], [255, 255]],                                 // 黑场抬升
  [[0, 0], [64, 40], [128, 128], [192, 220], [255, 255]],// S 曲线
  [[0, 0], [100, 180], [160, 80], [255, 255]],           // 含负斜率段(反向)
  [[0, 0], [128, 200], [255, 255]],                      // 3 点强提
];

const luts = CASES.map(pts => PhotoEdit._curveLut(pts));
process.stdout.write(JSON.stringify({ cases: CASES, luts }));
