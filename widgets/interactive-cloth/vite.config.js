import { defineConfig } from 'vite';
import { resolve } from 'node:path';

const root = resolve(__dirname);

export default defineConfig({
  root,
  server: {
    port: Number(process.env.PORT) || 5299,
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'es2020',
    rollupOptions: {
      // Two independent entry points -> two independent bundles.
      // The public bundle must never pull in Studio/admin code, so they
      // are kept as separate Rollup inputs rather than one multi-page
      // build sharing a single graph.
      input: {
        'sonya-cloth': resolve(root, 'src/public-entry.js'),
        'sonya-cloth-studio': resolve(root, 'src/studio-entry.js'),
      },
      output: {
        format: 'es',
        entryFileNames: '[name].js',
        chunkFileNames: 'chunks/[name]-[hash].js',
        assetFileNames: 'assets/[name]-[hash][extname]',
      },
    },
  },
});
