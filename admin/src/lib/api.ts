/**
 * Cliente HTTP del panel.
 *
 * Todas las llamadas son same-origin (`/api/...`) y viajan con la cookie de
 * sesión HttpOnly. Para métodos que mutan estado se añade el token CSRF que
 * FastAPI entrega en la cookie `wa_csrf` (patrón double-submit).
 */

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

function readCookie(name: string): string | null {
  const match = document.cookie.match(new RegExp(`(?:^|;\\s*)${name}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : null;
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? 'GET').toUpperCase();
  const headers = new Headers(init.headers);

  // Solo se declara JSON cuando el cuerpo es un string; con FormData el
  // navegador debe fijar el boundary del multipart.
  if (typeof init.body === 'string' && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const token = readCookie('wa_csrf');
    if (token) headers.set('X-CSRF-Token', token);
  }

  const response = await fetch(`/api${path}`, {
    credentials: 'include',
    ...init,
    headers,
  });

  if (response.status === 401) {
    if (!location.pathname.startsWith('/login')) {
      location.href = '/login/';
    }
    throw new ApiError('Sesión expirada.', 401);
  }

  if (!response.ok) {
    let message = response.statusText || `Error ${response.status}`;
    try {
      const data = await response.json();
      if (typeof data?.detail === 'string') message = data.detail;
      else if (Array.isArray(data?.detail)) {
        message = data.detail.map((d: { msg?: string }) => d.msg ?? 'Dato inválido').join(' · ');
      }
    } catch {
      /* respuesta sin JSON */
    }
    throw new ApiError(message, response.status);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export function toast(message: string, kind: 'ok' | 'error' = 'ok'): void {
  let node = document.querySelector<HTMLDivElement>('.toast');
  if (!node) {
    node = document.createElement('div');
    node.className = 'toast';
    document.body.appendChild(node);
  }
  node.textContent = message;
  node.classList.toggle('error', kind === 'error');
  node.classList.add('show');
  window.setTimeout(() => node?.classList.remove('show'), 3600);
}

export function money(value: number, currency = 'CLP'): string {
  const fractionDigits = currency === 'CLP' ? 0 : 2;
  return new Intl.NumberFormat('es-CL', {
    style: 'currency',
    currency,
    maximumFractionDigits: fractionDigits,
  }).format(value ?? 0);
}

export function fmtDate(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString('es-CL', {
    day: '2-digit',
    month: '2-digit',
    year: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/** Crea un elemento con texto seguro (evita innerHTML con datos de clientes). */
export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Record<string, string> = {},
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text !== undefined) node.textContent = text;
  return node;
}

export function statusBadge(status: string): HTMLSpanElement {
  const map: Record<string, { label: string; cls: string }> = {
    draft: { label: 'Borrador', cls: 'muted' },
    confirmed: { label: 'Confirmado', cls: 'ok' },
    shipped: { label: 'Enviado', cls: 'warn' },
    delivered: { label: 'Entregado', cls: 'ok' },
    cancelled: { label: 'Cancelado', cls: 'err' },
    pending: { label: 'Pendiente', cls: 'warn' },
    processing: { label: 'Procesando', cls: 'warn' },
    ready: { label: 'Listo', cls: 'ok' },
    error: { label: 'Error', cls: 'err' },
  };
  const item = map[status] ?? { label: status, cls: 'muted' };
  const span = document.createElement('span');
  span.className = `badge ${item.cls}`;
  span.textContent = item.label;
  return span;
}