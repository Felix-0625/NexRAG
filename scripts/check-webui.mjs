// Compile every Svelte component shipped by the maintained patch chain.
// Use Open WebUI's pinned compiler, never a globally installed package.
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
for (const name of ['tenant-selector.diff', 'ops-dashboard.diff', 'workspace.diff']) {
  const patch = fs.readFileSync(path.join(patchRoot, name), 'utf8');
  for (const match of patch.matchAll(/^\+\+\+ b\/(.+\.svelte)$/gm)) {
    components.add(match[1].trim());
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
