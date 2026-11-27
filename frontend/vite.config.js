import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

function addServiceWorkerHeader(server) {
  server.middlewares.use((request, response, next) => {
    if (request.url?.split('?')[0] === '/sw.js') {
      response.setHeader('Cache-Control', 'no-cache');
    }
    next();
  });
}

const serviceWorkerHeaders = {
  name: 'service-worker-no-cache',
  configureServer: addServiceWorkerHeader,
  configurePreviewServer: addServiceWorkerHeader,
};

export default defineConfig({
  plugins: [react(), serviceWorkerHeaders],
  server: {
    headers: { 'Cache-Control': 'no-cache' },
  },
  build: {
    rollupOptions: {
      output: {
        entryFileNames: 'assets/app.js',
        chunkFileNames: 'assets/[name].js',
        manualChunks(id) {
          if (id.includes('node_modules/jsqr')) return 'jsqr';
        },
        assetFileNames: asset => asset.name?.endsWith('.css')
          ? 'assets/app.css'
          : 'assets/[name][extname]',
      },
    },
  },
});
