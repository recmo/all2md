import { createHash } from 'node:crypto';
import { readdirSync, readFileSync } from 'node:fs';
import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';
const version = createHash('sha256');
for (const path of [
  'package.json',
  'pnpm-lock.yaml',
  'svelte.config.js',
  ...readdirSync('src', { recursive: true, withFileTypes: true })
    .filter((entry) => entry.isFile())
    .map((entry) => `${entry.parentPath}/${entry.name}`)
    .sort()
])
  version.update(path).update(readFileSync(path));
export default {
  preprocess: vitePreprocess(),
  kit: {
    version: { name: version.digest('hex').slice(0, 16) },
    adapter: adapter({ fallback: 'index.html' }),
    csp: {
      mode: 'hash',
      directives: {
        'script-src': ['self'],
        'object-src': ['none'],
        'base-uri': ['self']
      }
    }
  }
};
