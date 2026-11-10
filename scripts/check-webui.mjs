// 编译维护补丁链中交付的所有 Svelte 组件。
// 使用 Open WebUI 锁定的编译器，不调用全局安装的包。
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const checkout = path.join(root, 'webui');
const require = createRequire(path.join(checkout, 'package.json'));
const { compile } = require('svelte/compiler');
const patchRoot = path.join(root, 'patches/open-webui/v0.11.4/nexrag');
const components = new Set();
for (const name of ['tenant-selector.diff', 'ops-dashboard.diff', 'workspace.diff', 'security-npm.diff', 'fonts.diff']) {
  const patch = fs.readFileSync(path.join(patchRoot, name), 'utf8');
  for (const match of patch.matchAll(/^\+\+\+ b\/(.+\.svelte)$/gm)) {
    components.add(match[1].trim());
  }
}
const fontFiles = fs.readdirSync(path.join(checkout, 'static/assets/fonts')).sort();
if (fontFiles.length !== 1 || fontFiles[0] !== 'Inter-Variable.ttf') {
  throw new Error(`Expected only Inter in static/assets/fonts, found: ${fontFiles.join(', ')}`);
}
for (const name of ['src/app.css', 'src/tailwind.css']) {
  if (fs.readFileSync(path.join(checkout, name), 'utf8').includes('Vazirmatn')) {
    throw new Error(`Unused Vazirmatn font reference remains in ${name}`);
  }
}
if (components.size === 0) throw new Error('The patch chain contains no Svelte components.');
let failed = 0;
for (const name of [...components].sort()) {
  const filename = path.resolve(checkout, name);
  if (!filename.startsWith(checkout + path.sep)) throw new Error('Invalid patch path');
  try {
    compile(fs.readFileSync(filename, 'utf8'), { filename, generate: false });
  } catch (error) {
    failed++;
    console.error(`${name}: ${error.message}`);
  }
}
if (failed) process.exitCode = 1;
else console.log(`Compiled ${components.size} patched Svelte components.`);
