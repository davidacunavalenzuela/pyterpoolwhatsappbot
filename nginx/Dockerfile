# =============================================================================
# Imagen de Nginx con el panel Astro ya compilado y copiado como estáticos.
#
# Por qué multi-stage: compilar Astro requiere Node y ~600 MB de dependencias,
# pero en runtime no se necesita Node. Copiando solo `dist/` al contenedor
# final, el panel no consume RAM ni CPU en el servidor de 4 GB.
# =============================================================================
FROM node:22-alpine AS admin-build

WORKDIR /app
# Copiar primero los manifiestos permite cachear `npm install` mientras el
# código del panel cambia.
COPY admin/package.json admin/package-lock.json* ./
RUN npm install --no-audit --no-fund

COPY admin/ ./
# `npm run build` genera HTML/CSS/JS en /app/dist (output estático).
RUN npm run build


FROM nginx:1.27-alpine AS runtime

# Configuración propia: proxy reverso + headers de seguridad + estáticos.
COPY nginx/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=admin-build /app/dist /usr/share/nginx/html

# wget (busybox) se usa en el healthcheck del compose.
EXPOSE 80
