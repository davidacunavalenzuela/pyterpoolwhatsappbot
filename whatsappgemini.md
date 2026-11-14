# Configuración del Proyecto: Integración con WhatsApp Business

Este documento detalla los nombres exactos de las variables y datos requeridos para replicar el proyecto de conexión con la API de Meta, manteniendo la consistencia con implementaciones previas en OpenCode.

## 1. Variables de Identificación de Meta

Estos valores provienen directamente del panel de desarrolladores de Meta (Meta for Developers):

*   **Identificador del número de teléfono:** `1387086514492751`
*   **Identificador de la cuenta de WhatsApp Business (WABA ID):** `2273785206893498`
*   **Número de destino (Prueba / Destinatario):** `56940437942`

## 2. Variables de Seguridad y Autenticación

Tokens y claves necesarias para validar el túnel y autorizar el envío de mensajes:

*   **Token de Verificación del Webhook:** `mi_token_secreto_123`
*   **Token de Acceso de la API (Bearer Token):** Se debe almacenar en la variable de entorno `WA_ACCESS_TOKEN` y configurarse a nivel de entorno o en un archivo `.env` (en implementaciones pasadas, se almacenó en el archivo de configuración `config.json` de OpenCode).
    *   *Nota:* Este token debe generarse desde el panel de **Paso 1. Pruébalo** > **Generar token de acceso**.

## 3. Parámetros del Servidor y Webhook

Configuración de la conexión local:

*   **Puerto del Servidor Local:** `3000`
*   **URL Base del Túnel Público (Cloudflare):** `https://back-grid-businesses-lucky.trycloudflare.com`
*   **Endpoint / Ruta del Webhook:** `/webhook`
*   **URL Completa del Webhook en Meta:** `https://back-grid-businesses-lucky.trycloudflare.com/webhook`

## 4. Endpoints de la API Graph utilizados

Rutas de la API para suscribir y enviar mensajes (utilizando la API Graph v22.0):

*   **Suscripción de Webhooks:** `/{WABA_ID}/subscribed_apps` (Ejemplo: `2273785206893498/subscribed_apps`)
*   **Envío de Mensajes:** `/{PHONE_NUMBER_ID}/messages` (Ejemplo: `1387086514492751/messages`)