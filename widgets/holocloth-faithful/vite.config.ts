import { defineConfig } from 'vite';
import { resolve } from 'node:path';
import react from '@vitejs/plugin-react';

// SONYA addition: multi-page build (studio.html + public.html) instead of
// upstream's single index.html — everything else (plugins, dev port) is
// unchanged. See PARITY_CHECKLIST.md.
export default defineConfig({
  plugins: [react()],
  server: {
    port: Number(process.env.PORT) || 5199,
  },
  build: {
    rollupOptions: {
      input: {
        studio: resolve(__dirname, 'studio.html'),
        public: resolve(__dirname, 'public.html'),
      },
    },
  },
});
