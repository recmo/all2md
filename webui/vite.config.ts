import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vitest/config';
export default defineConfig({
  plugins: [sveltekit()],
  server: {
    proxy: {
      '^/(?!webui(?:/|$))': {
        target: process.env.MDSTORE_URL || 'http://127.0.0.1:3131',
        changeOrigin: true,
        configure: (proxy) =>
          proxy.on('proxyReq', (outgoing, incoming) => {
            const origin = incoming.headers.origin;
            const host = incoming.headers.host;
            // Preserve the browser's same-origin check across the development proxy.
            // Foreign origins remain untouched so mdstore rejects them.
            if (origin === `http://${host}` || origin === `https://${host}`) {
              const target = new URL(
                process.env.MDSTORE_URL || 'http://127.0.0.1:3131'
              );
              outgoing.setHeader('origin', target.origin);
              outgoing.setHeader(
                'x-forwarded-proto',
                new URL(origin).protocol.slice(0, -1)
              );
            }
          })
      }
    }
  },
  test: { environment: 'jsdom', include: ['src/**/*.test.ts'] }
});
