// 审计完整 npm 锁，仅允许已逐条核对的可选浏览器 TTS 高危公告。
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const npmPaths = [
  path.join(path.dirname(process.execPath), 'node_modules/npm/bin/npm-cli.js'),
  path.resolve(path.dirname(process.execPath), '../lib/node_modules/npm/bin/npm-cli.js')
];
const npm = npmPaths.find((candidate) => fs.existsSync(candidate));
if (!npm) throw new Error('Use the project Node 22 runtime or actions/setup-node.');

const result = spawnSync(process.execPath, [
  npm, 'audit', '--registry=https://registry.npmjs.org', '--json'
], { cwd: path.join(root, 'webui'), encoding: 'utf8', maxBuffer: 8 * 1024 * 1024 });
if (result.error) throw result.error;
const report = JSON.parse(result.stdout);
if (report.error || !report.metadata?.vulnerabilities || !report.vulnerabilities) {
  throw new Error('npm audit did not return a valid advisory report.');
}
if (![0, 1].includes(result.status)) throw new Error('npm audit did not complete.');

const output = path.join(root, '.local-run/audits/npm-release.json');
fs.mkdirSync(path.dirname(output), { recursive: true });
fs.writeFileSync(output, JSON.stringify(report, null, 2) + '\n');

// 2026-11-07 复核。仅浏览器 Kokoro worker 使用该链，详见安全验收记录。
// 新的公告、包名、严重级别均须重新审核。
const reviewedPackages = new Set(['sharp', '@huggingface/transformers', 'kokoro-js']);
const reviewedAdvisories = new Set([
  'https://github.com/advisories/GHSA-f88m-g3jw-g9cj',
  'https://github.com/advisories/GHSA-rgj7-g3m4-5g8c',
  'https://github.com/advisories/GHSA-wq5f-xc86-pv6w'
]);

function advisories(name, seen = new Set()) {
  if (seen.has(name)) return [];
  const next = new Set([...seen, name]);
  return (report.vulnerabilities[name]?.via ?? []).flatMap((entry) =>
    typeof entry === 'string' ? advisories(entry, next) : [entry.url]
  );
}

const blocked = Object.entries(report.vulnerabilities).filter(([name, item]) => {
  if (item.severity === 'critical') return true;
  if (item.severity !== 'high') return false;
  const urls = advisories(name);
  return !reviewedPackages.has(name) || !urls.length ||
    urls.some((url) => !reviewedAdvisories.has(url));
});
console.log(JSON.stringify({ counts: report.metadata.vulnerabilities, blocked: blocked.map(([name]) => name), report: output }));
if (blocked.length) process.exitCode = 1;
