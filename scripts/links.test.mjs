// Every relative link in the skill's Markdown must resolve: agents follow these paths literally.
// Run: node --test scripts/links.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
function markdownFiles(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap(entry => {
    if (entry.name.startsWith('.') || entry.name === 'node_modules') return [];
    const full = path.join(dir, entry.name);
    return entry.isDirectory() ? markdownFiles(full) : entry.name.endsWith('.md') ? [full] : [];
  });
}

test('relative Markdown links resolve to files in the repository', () => {
  const broken = [];
  for (const file of markdownFiles(root)) {
    for (const [, target] of fs.readFileSync(file, 'utf8').matchAll(/\]\(([^)\s]+)\)/g)) {
      if (/^(?:[a-z][a-z0-9+.-]*:|#)/i.test(target)) continue;
      const resolved = path.resolve(path.dirname(file), target.split('#')[0]);
      if (!fs.existsSync(resolved)) broken.push(`${path.relative(root, file)} -> ${target}`);
    }
  }
  assert.deepEqual(broken, []);
});
