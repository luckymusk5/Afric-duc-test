import {
  getDeviceConfiguration,
  readPendingEvents,
  removeAcknowledgedEvents,
} from './db';
import { updateServerTime } from './clock';

const API = import.meta.env.VITE_API_URL || 'http://localhost:8000/api';
const BATCH_SIZE = 5;
const REQUEST_TIMEOUT_MS = 3000;
const RETRY_LIMIT_SECONDS = 60;

let fallbackLock = false;
let retryDelaySeconds = 2;
let retryTimer = null;
let authenticationBlocked = false;
let refreshQueueCount = () => {};
let requestLogin = () => {};

class AuthenticationRequired extends Error {}

async function csrfToken() {
  const response = await fetch(`${API}/auth/csrf/`, { credentials: 'include' });
  if (!response.ok) throw new Error('Impossible de renouveler le jeton CSRF.');
  const { csrfToken: token } = await response.json();
  if (!token) throw new Error('Le serveur n’a pas fourni de jeton CSRF.');
  localStorage.setItem('schoolLogsCsrf', token);
  return token;
}

function scheduleRetry() {
  if (retryTimer !== null) clearTimeout(retryTimer);
  retryTimer = setTimeout(() => {
    retryTimer = null;
    synchronizePending();
  }, retryDelaySeconds * 1000);
  retryDelaySeconds = Math.min(retryDelaySeconds * 2, RETRY_LIMIT_SECONDS);
}

async function postBatch(events) {
  const configuration = await getDeviceConfiguration();
  if (!configuration?.device_token) {
    throw new AuthenticationRequired('Ce portail doit être enrôlé par un responsable.');
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  const requestStartedAt = performance.now();
  try {
    const csrf = await csrfToken();
    const response = await fetch(`${API}/passages/sync`, {
      method: 'POST',
      credentials: 'include',
      signal: controller.signal,
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': csrf,
        'X-Device-Token': configuration.device_token,
      },
      body: JSON.stringify({ events }),
    });
    if (response.status === 401 || response.status === 403) {
      authenticationBlocked = true;
      throw new AuthenticationRequired('Reconnectez-vous pour synchroniser les passages.');
    }
    if (!response.ok) {
      throw new Error(`Synchronisation refusée (${response.status}).`);
    }
    const result = await response.json();
    await updateServerTime(result.server_time, requestStartedAt);
    const eventIds = new Set(events.map(event => event.event_id));
    const acknowledgements = (result.acks || []).filter(ack =>
      eventIds.has(ack.event_id) &&
      ['created', 'duplicate'].includes(ack.status),
    );
    if (acknowledgements.length) {
      await removeAcknowledgedEvents(acknowledgements.map(ack => ack.event_id));
      await refreshQueueCount();
    }
    retryDelaySeconds = 2;
    if (retryTimer !== null) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
    return acknowledgements;
  } finally {
    clearTimeout(timer);
  }
}

async function sendWithLock(events, currentEventId = null, onVerdict = () => {}) {
  const send = async () => {
    const acknowledgements = await postBatch(events);
    if (currentEventId) {
      const current = acknowledgements.find(ack => ack.event_id === currentEventId);
      if (current) onVerdict(current.resultat);
    }
    return acknowledgements;
  };
  if (navigator.locks?.request) {
    return navigator.locks.request('sync', { ifAvailable: true }, lock => {
      return lock ? send() : [];
    });
  }
  if (fallbackLock) return [];
  fallbackLock = true;
  try {
    return await send();
  } finally {
    fallbackLock = false;
  }
}

export async function synchronizePending() {
  if (authenticationBlocked) return;
  try {
    let pending = await readPendingEvents(100);
    while (pending.length) {
      const batch = pending.slice(0, BATCH_SIZE);
      const acknowledgements = await sendWithLock(batch);
      if (!acknowledgements.length) break;
      if (acknowledgements.length < batch.length) break;
      pending = await readPendingEvents(100);
    }
    await refreshQueueCount();
  } catch (error) {
    if (error instanceof AuthenticationRequired) {
      requestLogin(error.message);
      return;
    }
    scheduleRetry();
  }
}

export async function queueAndSend(event, onVerdict) {
  if (authenticationBlocked) return;
  try {
    const acknowledgements = await sendWithLock([event], event.event_id, onVerdict);
    if (!acknowledgements.length) scheduleRetry();
  } catch (error) {
    if (error instanceof AuthenticationRequired) {
      requestLogin(error.message);
      return;
    }
    scheduleRetry();
  }
}

export function startSynchronization({ onQueueCount, onAuthenticationRequired } = {}) {
  authenticationBlocked = false;
  refreshQueueCount = onQueueCount || refreshQueueCount;
  requestLogin = onAuthenticationRequired || requestLogin;
  const sync = () => { synchronizePending(); };
  window.addEventListener('online', sync);
  const interval = setInterval(sync, 15000);
  sync();
  return () => {
    window.removeEventListener('online', sync);
    clearInterval(interval);
    if (retryTimer !== null) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
  };
}
