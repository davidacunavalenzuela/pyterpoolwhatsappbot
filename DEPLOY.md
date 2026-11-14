# Despliegue en producción

Runbook para levantar el asistente en un VPS de 4 GB con Docker, Nginx y
Cloudflare Tunnel. Todos los comandos asumen Linux (Debian/Ubuntu) y
`docker compose` v2.

## 1. Arquitectura

```
Internet
   |
Cloudflare (TLS, DNS, Access opcional)
   |
cloudflared  --(red interna)-->  nginx:80
                                   |-- /        panel Astro (estático)
                                   |-- /api/*   FastAPI
                                   |-- /webhook WhatsApp Cloud API
                                   |
                                  api:8000  -----> postgres:5432 (pgvector)
                                  worker       (cola en Postgres)
```

- Nginx publica el 8080 **solo en loopback** del host; el tráfico público entra
  por Cloudflare Tunnel, que conecta a `nginx:80` por la red interna.
- La API y el worker comparten imagen pero son procesos separados. El worker
  absorbe los picos de LLM/embeddings sin bloquear el webhook.
- No hay Redis: la cola vive en `inbound_events` y se consume con
  `FOR UPDATE SKIP LOCKED`.

Límites de memoria configurados: Postgres 1024 MB, API 640 MB, worker 896 MB,
Nginx 128 MB, cloudflared 128 MB (~2,8 GB de techo). El resto queda para el
sistema y la swap.

## 2. Requisitos

- VPS 2 vCPU / 4 GB RAM / 40 GB SSD, Docker Engine + Compose v2.
- Dominio gestionado en Cloudflare (plan gratuito sirve).
- App de Meta con WhatsApp Cloud API y número de prueba o productivo.
- API key de Groq (texto/audio) y de Google AI Studio (solo visión).
- Acceso SSH con clave.

## 3. Preparar el host

```bash
# Swap: evita que el OOM killer mate Postgres durante el primer ingest o un
# pico de embeddings. 2 GB es suficiente con este stack.
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
sysctl vm.swappiness   # opcional: bajarlo a 10

# Docker (si no está instalado)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"   # cerrar y volver a entrar

# Firewall: solo SSH. El tráfico web entra por el túnel (saliente).
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw enable
```

> Ojo: Docker inserta sus reglas por delante de ufw. El compose ya publica el
> 8080 solo en loopback (`127.0.0.1:8080:80`), así que no queda expuesto aunque
> el firewall falle; el túnel entra por la red interna (`nginx:80`).

## 4. Código y configuración

```bash
sudo mkdir -p /opt/whatsappbot && sudo chown "$USER":"$USER" /opt/whatsappbot
git clone <repo> /opt/whatsappbot
cd /opt/whatsappbot

cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # SESSION_SECRET
```

Completar `.env` (nunca versionarlo):

| Variable | Nota |
|---|---|
| `APP_ENV=prod` | Activa validación estricta y cookies `Secure`. |
| `PUBLIC_BASE_URL=https://bot.tudominio.com` | Sin `/webhook`. |
| `POSTGRES_PASSWORD` | Larga y aleatoria. |
| `DATABASE_URL` | Debe repetir la misma clave y usar host `postgres`. |
| `SESSION_SECRET` | 64+ caracteres. |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | Credenciales del panel (10+ caracteres). |
| `COOKIE_SECURE=true` | Obligatorio con HTTPS. |
| `WA_ACCESS_TOKEN` / `WA_APP_SECRET` | Rotar los tokens filtrados en `whatsappgemini.md`. |
| `WA_VERIFY_TOKEN` | Debe coincidir con el que se escriba en Meta. |
| `GROQ_API_KEY` / `GEMINI_API_KEY` | Texto/audio y visión respectivamente. |
| `TUNNEL_TOKEN` | Token del túnel nombrado de Cloudflare. |

Con `APP_ENV=prod` la API falla al arrancar si falta un secreto crítico o si
quedó un placeholder (`completar`, `cambia`, `rotar`). Es intencional: es mejor
fallar en el arranque que descubrir el problema con el webhook en vivo.

Validar la interpolación del compose sin arrancar nada:

```bash
docker compose config -q
```

### Permisos de `docs`

El backend corre como uid/gid `10001`. El directorio `./docs` es un bind mount,
así que en Linux hay que darle permiso de escritura (subidas del panel e
ingest):

```bash
sudo chown -R 10001:10001 docs
```

## 5. Build y arranque

```bash
docker compose build          # API+worker y Nginx con el panel Astro compilado
docker compose up -d          # sin el perfil tunnel todavía
docker compose ps
```

Esperado: `postgres`, `api`, `nginx` en `healthy` y `worker` en `running`.
El `entrypoint.sh` aplica `alembic upgrade head` en API y worker; el worker
espera a que la API esté sana para no competir por migraciones en frío.

```bash
curl -fsS http://127.0.0.1:8080/health   # {"status":"ok"} (incluye SELECT 1)
```

## 6. Primer contenido

```bash
# Usuario del panel + ajustes por defecto (idempotente).
docker compose exec -T api python -m app.cli seed

# Opcional: crear/rotar otro administrador sin tocar el .env.
docker compose exec -T api python -m app.cli create-admin \
  --email admin@tudominio.com --password 'clave-larga-y-unica'

# Copiar manuales a ./docs y generar chunks + embeddings.
# Se ejecuta en un contenedor del worker porque ahí hay 896 MB de límite:
# el primer uso descarga el modelo ONNX (~220 MB) al volumen `models`.
docker compose run --rm --no-deps worker cli ingest --force
```

`--force` reindexa aunque el checksum no cambie; úsalo tras cambiar
`EMBEDDINGS_MODEL` (la columna es `vector(384)`; otro tamaño exige migración).

Comprobar datos y cola:

```bash
docker compose exec -T api python -m app.cli status
docker compose logs --tail 30 worker
```

## 7. Cloudflare Tunnel

1. Zero Trust → Networks → Tunnels → crear túnel.
2. Public Hostname: `bot.tudominio.com` → servicio `HTTP` → `nginx:80`.
3. Copiar el token del conector a `TUNNEL_TOKEN` en `.env`.
4. Arrancar el perfil:

```bash
docker compose --profile tunnel up -d
docker compose logs -f cloudflared   # debe mostrar "Registered tunnel connection"
```

Cloudflare crea el CNAME y termina TLS; el origen es HTTP interno. No abras
puertos 80/443 en el VPS.

## 8. Webhook de Meta

1. App de Meta → WhatsApp → Configuration:
   - Callback URL: `https://bot.tudominio.com/webhook`
   - Verify token: el mismo `WA_VERIFY_TOKEN`.
2. Suscribir el campo `messages`.
3. Suscribir la app al WABA (necesita `WA_ACCESS_TOKEN`):

```bash
docker compose exec -T api python -m app.cli subscribe
```

4. Enviar un mensaje real al número y verificar:

```bash
docker compose logs -f api worker
docker compose exec -T postgres psql -U wa -d wa \
  -c "SELECT kind, status, attempts, created_at FROM inbound_events ORDER BY id DESC LIMIT 5;"
```

El POST exige `X-Hub-Signature-256` válida; sin `WA_APP_SECRET` correcto
responde 403 (en `prod` no hay bypass). Los reintentos de Meta se deduplican
por `wa_message_id`.

## 9. Verificación funcional

```bash
# Panel (login con ADMIN_EMAIL/PASSWORD) y API
curl -I https://bot.tudominio.com/login/
curl -s https://bot.tudominio.com/api/stats -o /dev/null -w '%{http_code}\n'  # 401/403 sin sesión

# Consulta RAG (desde el panel o con sesión)
# GET /api/knowledge/search?q=como%20limpio%20la%20cafetera

# Pedido por WhatsApp: carrito -> confirmar (descuenta stock) -> cancelar (repone)
```

Checklist mínimo antes de dar por terminado:

- [ ] `/health` 200 y `nginx` sirviendo el panel por HTTPS.
- [ ] Login, logout (con CSRF) y `/api/auth/me`.
- [ ] Producto de prueba visible desde WhatsApp y pedido confirmado.
- [ ] Manual en `docs/` respondido por RAG.
- [ ] Audio transcrito y respuesto.
- [ ] Imagen descrita (Gemini) sin enviar historial.
- [ ] Firma del webhook rechazando peticiones sin HMAC.
- [ ] Backup manual ejecutado y verificado.

## 10. Operación

### Logs y estado

```bash
docker compose ps
docker compose logs -f api
docker compose logs -f worker
docker compose exec -T api python -m app.cli status
```

### Cola

```bash
docker compose exec -T postgres psql -U wa -d wa -c \
  "SELECT status, count(*) FROM inbound_events GROUP BY 1 ORDER BY 1;"
```

Un evento `failed` agotó `worker_max_attempts`; los `processing` viejos se
recuperan solos tras `worker_stuck_after_seconds` (300 s). Para reprocesar:

```bash
docker compose exec -T postgres psql -U wa -d wa -c \
  "UPDATE inbound_events SET status='pending', attempts=0 WHERE id = 123;"
```

### Backups

```bash
mkdir -p backups
docker compose exec -T postgres pg_dump -U wa -d wa -Fc > "backups/wa-$(date +%F).dump"
```

Cron diario a las 03:00 con retención de 14 días (los `%` van escapados):

```cron
0 3 * * * cd /opt/whatsappbot && docker compose exec -T postgres pg_dump -U wa -d wa -Fc > backups/wa-$(date +\%F).dump && find backups -name '*.dump' -mtime +14 -delete
```

Restaurar (parar escritores primero):

```bash
docker compose stop api worker
docker compose exec -T postgres pg_restore -U wa -d wa --clean --if-exists --no-owner < backups/wa-2026-11-13.dump
docker compose start api worker
```

Guardar también una copia segura de `.env` (contiene todos los secretos) y
probar la restauración en un entorno de staging, no directamente en producción.

### Actualizar

```bash
cd /opt/whatsappbot
git pull --ff-only
docker compose build
docker compose up -d            # alembic corre solo en api y worker
docker compose ps
docker compose logs --tail 50 api worker
```

Las migraciones no se revierten automáticamente: si el cambio de esquema es
incompatible, el rollback es `git checkout <tag>` + restaurar el dump.

### RAG

- Subidas desde el panel (`/knowledge`) se encolan y el worker indexa.
- Archivos copiados a mano en `docs/` requieren `cli ingest` (o `--force`).
- Si el ingest falla por memoria en la API, ejecútalo en el worker
  (`docker compose run --rm --no-deps worker cli ingest`).
- El catálogo de productos se reindexa automáticamente al cambiar productos.

## 11. Seguridad

- Rotar `WA_ACCESS_TOKEN` y agregar `WA_APP_SECRET` real: estaban en el
  documento de origen y deben considerarse comprometidos.
- `SESSION_SECRET` único, `COOKIE_SECURE=true` y cookies `HttpOnly`.
- Los POST del panel exigen CSRF (`X-CSRF-Token`); el logout también.
- OpenAPI deshabilitado con `APP_ENV=prod`.
- Nginx aplica CSP, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`,
  `Permissions-Policy` y oculta la versión.
- Cerrar el 8080 al exterior (loopback o firewall) y no exponer Postgres.
- Actualizaciones automáticas del SO (`unattended-upgrades`) y SSH con clave.
- Opcional: Cloudflare Access para el panel, con política **Bypass** en
  `/webhook` (y `/health`) para que Meta siga entrando sin autenticación.

## 12. Problemas frecuentes

| Síntoma | Causa probable | Acción |
|---|---|---|
| `502` en el panel | API no sana | `docker compose logs api`, revisar `/health` y credenciales de Postgres |
| Webhook 403 | `WA_APP_SECRET` no coincide o hay espacios | Copiar el secreto exacto y recrear `api` |
| Meta no valida la URL | `WA_VERIFY_TOKEN` distinto | Corregir `.env` y reintentar el handshake |
| Meta reintenta y duplica | Respuesta > 5 s | Revisar logs de Nginx/API; el procesamiento va en worker |
| `429` de Groq | Límite free tier | Subir `LLM_MIN_INTERVAL_SECONDS`; usar fallback |
| `connection is closed` en ingest | Transacción abierta > 30 s (`idle_in_transaction_session_timeout`) | No llamar APIs externas dentro de la transacción; commit antes |
| Worker no avanza | Evento `failed` o `processing` viejo | Reencolar con el `UPDATE` de la sección 10 |
| Login en bucle | `COOKIE_SECURE=true` sobre HTTP | Usar HTTPS del túnel o `false` solo en local |
| `403 Token CSRF inválido` | Cookie `wa_csrf` ausente/expirada | Borrar cookies y volver a iniciar sesión |
| Postgres `password authentication failed` | Clave cambiada en `.env` sin `ALTER USER` | Recrear el volumen (destructivo) o cambiar la clave en el motor |
| `nginx -t` falla con `host not found: api` en el host | Validación fuera de la red del compose | Probar dentro de la red: `docker run --rm --network whatsappbot_internal ... nginx -t` |
| Aviso de `mean pooling` al cargar embeddings | Cambio de comportamiento de fastembed | Benigno; no afecta la dimensión 384 |

## 13. Desarrollo local (referencia)

```bash
# Windows/Linux con Docker Desktop: override con autoreload y puertos abiertos
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

Diferencias: `APP_ENV=dev`, `COOKIE_SECURE=false`, API con `--reload`, Postgres
expuesto en 5432 y `nginx.dev.conf` sin CSP (solo para local). El webhook acepta
peticiones sin firma en dev (nunca en prod).

Pruebas y lint antes de desplegar:

```bash
cd backend && ruff check app tests alembic && pytest -q
cd ../admin && npm ci && npm run build
```