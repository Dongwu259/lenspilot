/**
 * trim-locales.js — afterPack hook for electron-builder
 * Removes all Electron locale files except zh-CN and en-US.
 * Saves ~36 MB from the packaged app.
 *
 * Usage in electron-builder.yml:
 *   afterPack: "./trim-locales.js"
 */
const fs = require('fs');
const path = require('path');

exports.default = async function (context) {
  const localesDir = path.join(context.appOutDir, 'locales');
  if (!fs.existsSync(localesDir)) {
    console.log('[trim-locales] No locales dir found, skipping');
    return;
  }

  const keep = new Set(['zh-CN.pak', 'en-US.pak', 'en.pak']);
  const files = fs.readdirSync(localesDir);
  let removed = 0;

  for (const f of files) {
    if (!keep.has(f) && f.endsWith('.pak')) {
      const p = path.join(localesDir, f);
      const size = fs.statSync(p).size;
      fs.unlinkSync(p);
      removed += size;
      console.log(`[trim-locales] Removed: ${f} (${(size / 1024).toFixed(0)} KB)`);
    }
  }

  const totalMB = (removed / 1048576).toFixed(1);
  console.log(`[trim-locales] Done — removed ${files.length - keep.size} files, saved ${totalMB} MB`);
};
