import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './tests',
  workers: 1,
  use: {
    baseURL: 'http://127.0.0.1:43132',
    viewport: { width: 1440, height: 1000 },
    launchOptions: { executablePath: process.env.CHROME_EXECUTABLE }
  },
  webServer: {
    command: 'node tests/daemon.mjs',
    url: 'http://127.0.0.1:43132/',
    reuseExistingServer: false,
    timeout: 30000
  }
});
