import react from '@vitejs/plugin-react';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';

const roadGraphPath = fileURLToPath(
  new URL('../backend/data/batam_graph.json', import.meta.url),
);
const roadGraphAssetPath = '/assets/batam-road-graph.json';

/**
 * Expose the one intended shared data artifact without making backend/data
 * (which can contain SQLite history files) a public directory.
 */
function bundledRoadGraph() {
  return {
    name: 'crossflow-bundled-road-graph',
    configureServer(server: import('vite').ViteDevServer) {
      server.middlewares.use(roadGraphAssetPath, (_request, response) => {
        response.setHeader('Content-Type', 'application/json; charset=utf-8');
        response.setHeader('Cache-Control', 'public, max-age=3600');
        response.end(readFileSync(roadGraphPath));
      });
    },
    generateBundle(this: import('rollup').PluginContext) {
      this.emitFile({
        type: 'asset',
        fileName: roadGraphAssetPath.slice(1),
        source: readFileSync(roadGraphPath),
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), bundledRoadGraph()],
  server: {
    port: 3000,
    host: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  test: {
    setupFiles: ['./src/test/setup.ts'],
  },
});
