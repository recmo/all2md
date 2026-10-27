import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';
export default defineConfig({
  plugins: [sveltekit()],
  server: {
    proxy: Object.fromEntries(
      ['/mcp', '/health', '/path'].map((path) => [
        path,
        {
          target: process.env.MDSTORE_URL || 'http://127.0.0.1:3131'
        }
      ])
    )
  }
});
