import { getSetting, setSetting } from './db';

let serverEpoch = null;
let performanceAnchor = null;

export async function restoreClock() {
  if (serverEpoch !== null) return true;
  const saved = await getSetting('lastServerTime');
  if (!saved) return false;
  if (typeof saved === 'string') {
    serverEpoch = Date.parse(saved);
  } else {
    const savedEpoch = Date.parse(saved.serverTime);
    const elapsed = Date.now() - saved.localTime;
    if (!Number.isFinite(savedEpoch) || !Number.isFinite(elapsed) || elapsed < 0) {
      return false;
    }
    serverEpoch = savedEpoch + elapsed;
  }
  performanceAnchor = performance.now();
  return Number.isFinite(serverEpoch);
}

export async function updateServerTime(serverTime, requestStartedAt = performance.now()) {
  const epoch = Date.parse(serverTime);
  if (!Number.isFinite(epoch)) {
    throw new Error('Heure serveur invalide.');
  }
  const receivedAt = performance.now();
  performanceAnchor = (requestStartedAt + receivedAt) / 2;
  serverEpoch = epoch + (receivedAt - requestStartedAt) / 2;
  await setSetting('lastServerTime', {
    serverTime: new Date(serverEpoch).toISOString(),
    localTime: Date.now(),
  });
}

export async function saveClockCheckpoint(serverTime = estimatedServerTime()) {
  // Un point récent réduit l’intervalle à extrapoler si l’onglet est rouvert hors ligne.
  await setSetting('lastServerTime', {
    serverTime: serverTime.toISOString(),
    localTime: Date.now(),
  });
}

export function estimatedServerTime() {
  if (serverEpoch === null || performanceAnchor === null) {
    throw new Error('L’horloge du portail doit être synchronisée avant le scan.');
  }
  return new Date(serverEpoch + (performance.now() - performanceAnchor));
}
