import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vitest/config';
export default defineConfig({
  plugins: [sveltekit()],
  server: {
    proxy: Object.fromEntries(
      ['/mcp', '/health'].map((path) => [
        path,
        {
          target: process.env.MDSTORE_URL || 'http://127.0.0.1:3131'
        }
      ])
    )
  },
  test: { environment: 'jsdom', include: ['src/**/*.test.ts'] }
});
