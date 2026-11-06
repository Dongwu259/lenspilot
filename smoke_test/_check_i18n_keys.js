// i18n key 完整性检查:每个 key 必须在 zh/en/ja 三处各出现一次
const fs = require('fs');
const path = require('path');
// P2-6b 语言包拆分后：默认拼接 i18n.js + i18n-data-{zh,en,ja}.js（argv 仍可指定单文件）
let src;
if (process.argv[2]) {
  src = fs.readFileSync(process.argv[2], 'utf8');
} else {
  const dir = 'src/js';
  const parts = [fs.readFileSync(path.join(dir, 'i18n.js'), 'utf8')];
  for (const l of ['zh', 'en', 'ja']) {
    const f = path.join(dir, `i18n-data-${l}.js`);
    if (fs.existsSync(f)) parts.push(fs.readFileSync(f, 'utf8'));
  }
  src = parts.join('\n');
}
const keys = [
  'navPhotoEdit', 'pepRelayGroup', 'pepRelaySelect', 'pepRelayBrowser', 'pepRelayCopy',
  'pepPickFiles', 'pepPickFolder', 'pepEmptyTitle', 'pepEmptyDesc', 'pepCount',
  'pepCompare', 'pepCompareN', 'pepCompareNeed2', 'pepCompareLimit', 'pepCompareHint',
  'pepCompareTip', 'pepRelaySelectEmpty', 'pepRelayBrowserEmpty', 'pepRelayCopyEmpty',
  'pepLoaded', 'pepFolderEmpty', 'pepFromSelect', 'pepFromBrowser', 'pepFromCopy', 'pepFromPick',
  'peCompare', 'peCompareTitle', 'peBefore', 'peAfter',
  'pexTitle', 'pexFormat', 'pexQuality', 'pexSize', 'pexSizeOrig', 'pexSizeCustom',
  'pexColor', 'pexColorOrig', 'pexWatermark', 'pexWmNone', 'pexWmText', 'pexWmTextLabel',
  'pexWmPos', 'pexPosBR', 'pexPosBL', 'pexPosTR', 'pexPosTL', 'pexPosC',
  'pexWmOpacity', 'pexDest', 'pexDestPl', 'pexStart', 'pexExporting', 'pexDone',
  'pexNoDest', 'pexFail', 'pexWmNoText', 'pepExport', 'pepSelAll', 'pepSelClear', 'pepSelCount',
  'pepAdded', 'pepAddedDup',
  'peLrSection', 'peVibrance', 'peWbTint', 'peClarity', 'peTexture', 'peDehaze',
  'peLevels', 'peLevelsBlack', 'peLevelsWhite', 'peMidtones',
  'peHslSection', 'peHslHue', 'peHslLum',
  'peHslRed', 'peHslOrange', 'peHslYellow', 'peHslGreen',
  'peHslAqua', 'peHslBlue', 'peHslPurple', 'peHslMagenta',
  'peGradingSection', 'peVignette', 'peGrain',
  'peGeo', 'peCrop', 'peRotateL', 'peRotateR', 'peFlipH', 'peFlipV', 'peStraighten',
  'peCropApply', 'peCropCancel', 'peRatioFree', 'peRatioOrig', 'peCropReset',
  'peHistogram', 'peZoomFit', 'peZoom100', 'peCopyEdits', 'peEditsCopied',
  'pepPaste', 'pepPasteTitle', 'pepPasteEmpty', 'pepPasteNoSel',
  'dashEditTitle', 'dashEditDesc', 'dashTetherTitle', 'dashTetherDesc',
  'peAiTone', 'peAiToneTitle', 'peAiToneApplied', 'peAiToneRule', 'peAiToneFail',
  'atSettings', 'atModelStatus', 'atStatusChecking', 'atDownload', 'atStatusReady',
  'atStatusNoEngine', 'atStatusNeedWeights', 'atStatusNeedTower', 'atStatusError',
  'atDlStarting', 'atDlDone', 'atDlError',
];
const missing = [];
for (const k of keys) {
  const n = (src.match(new RegExp('[{,\\s]' + k + ':', 'g')) || []).length;
  if (n !== 3) missing.push(`${k} x${n}`);
}
console.log(missing.length ? 'MISSING: ' + missing.join(', ') : `${keys.length} 个新 key × 3 语言 全部齐`);
