import { defineConfig } from 'astro/config';

// Output estático a propósito: el panel es HTML + fetch a la API y Nginx lo
// sirve sin un runtime Node, ahorrando RAM en el servidor de 4 GB.
export default defineConfig({
  output: 'static',
  build: {
    format: 'directory',
  },
  server: {
    port: 4321,
  },
  // Las rutas se resuelven con try_files en Nginx (/products/ -> index.html).
  trailingSlash: 'ignore',
});
