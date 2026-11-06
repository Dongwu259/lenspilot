// P3 前端 Amount 缩放数值断言：PhotoEdit._scalePresetEdits
// 运行: node smoke_test/test_p3_preset_scale.mjs
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const src = readFileSync(join(root, 'electron_app', 'src', 'js', 'photo-edit.js'), 'utf8');
const PhotoEdit = new Function(`${src}; return PhotoEdit;`)();

const PRESET = {
  exposure: 0.4, contrast: 1.2, denoise: 8, grayscale: true,
  vignette: [0.4, 0.5, 0.5], grain: [0.3, 1.1],
  levels: [20, 235, 1.2],
  curves: { rgb: [[0, 20], [128, 150], [255, 255]] },
  hsl: { blue: [10, 0.2, -0.05] },
  color_grading: { shadows: [210, 0.2, -0.04] },
  masks: [{ id: 1, type: 'sky', invert: false, amount: 1.0, params: {},
            adjustments: { dehaze: 0.3, saturation: 0.12 } }],
};

function approx(a, b, eps = 1e-6) { return Math.abs(a - b) < eps; }

// 100% 原样
const s100 = PhotoEdit._scalePresetEdits(PRESET, 1.0);
assert(s100 !== PRESET && approx(s100.contrast, 1.2) && s100.grayscale === true);

// 50%：偏移减半；布尔≥50% 生效；曲线向对角线收缩；蒙版几何保留
const s50 = PhotoEdit._scalePresetEdits(PRESET, 0.5);
assert(approx(s50.exposure, 0.2));                    // 0 + 0.4*0.5
assert(approx(s50.contrast, 1.1));                    // 1 + 0.2*0.5
assert(approx(s50.denoise, 4));
assert(s50.grayscale === true);                       // >=50% 生效
assert(approx(s50.vignette[0], 0.2) && s50.vignette[1] === 0.5);
assert(approx(s50.grain[0], 0.15) && s50.grain[1] === 1.1);
assert(approx(s50.levels[0], 10) && s50.levels[1] === 245 && approx(s50.levels[2], 1.1));
assert(approx(s50.curves.rgb[1][1], 139));            // 128 + 22*0.5 = 139
assert(s50.curves.rgb[0][0] === 0 && s50.curves.rgb[2][0] === 255);
assert(approx(s50.hsl.blue[1], 0.1) && s50.hsl.blue[0] === 5);
assert(approx(s50.color_grading.shadows[1], 0.1) && s50.color_grading.shadows[0] === 210);
assert(s50.masks.length === 1 && s50.masks[0].type === 'sky' && s50.masks[0].params);
assert(approx(s50.masks[0].adjustments.dehaze, 0.15));

// 30%：布尔不生效
const s30 = PhotoEdit._scalePresetEdits(PRESET, 0.3);
assert(s30.grayscale === undefined);
assert(approx(s50.curves.rgb[2][1], 255));            // 对角线上的点不动

// 0%：全部消隐（无键残留）
const s0 = PhotoEdit._scalePresetEdits(PRESET, 0.0);
assert(Object.keys(s0).length === 0, JSON.stringify(s0));

// 200%：偏移加倍，仍在对角线同侧（单调性保持）
const s200 = PhotoEdit._scalePresetEdits(PRESET, 2.0);
assert(approx(s200.exposure, 0.8));
const xs200 = s200.curves.rgb.map(p => p[0]);
assert(xs200.every((x, i) => i === 0 || x > xs200[i - 1]));

function assert(cond, msg) { if (!cond) { console.error('FAIL: ' + (msg || '')); process.exit(1); } }
console.log('[ok] preset amount: 0%/30%/50%/100%/200% 缩放全部符合预期');
console.log('P3 PRESET SCALE SMOKE PASSED');
