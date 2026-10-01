const {defineConfig} = require('@playwright/test');

module.exports = defineConfig({
  testDir: './tests/ui',
  fullyParallel: true,
  use: {baseURL: 'http://127.0.0.1:8766', browserName: 'chromium'},
  projects: [
    {name: 'desktop', use: {viewport: {width: 1440, height: 1000}}},
    {name: 'narrow', use: {viewport: {width: 390, height: 844}}},
  ],
  webServer: {
    command: 'python3 tests/ui/server.py',
    url: 'http://127.0.0.1:8766',
    reuseExistingServer: false,
  },
});
