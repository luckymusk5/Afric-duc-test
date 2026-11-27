const DATABASE_NAME = 'afric-educ-passage';
const DATABASE_VERSION = 1;
const EVENT_STORE = 'events';
const SETTINGS_STORE = 'settings';

function openDatabase() {
  return new Promise((resolve, reject) => {
    if (!('indexedDB' in window)) {
      reject(new Error('IndexedDB n’est pas disponible dans ce navigateur.'));
      return;
    }

    const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(EVENT_STORE)) {
        const events = database.createObjectStore(EVENT_STORE, { keyPath: 'event_id' });
        events.createIndex('token', 'token', { unique: false });
        events.createIndex('local_time', 'local_time', { unique: false });
      }
      if (!database.objectStoreNames.contains(SETTINGS_STORE)) {
        database.createObjectStore(SETTINGS_STORE, { keyPath: 'key' });
      }
    };
    request.onsuccess = () => {
      const database = request.result;
      database.onversionchange = () => database.close();
      resolve(database);
    };
    request.onerror = () => reject(request.error || new Error('Ouverture IndexedDB impossible.'));
    request.onblocked = () => reject(new Error('La base locale est bloquée par un autre onglet.'));
  });
}

function runTransaction(storeNames, mode, action) {
  return openDatabase().then(database => new Promise((resolve, reject) => {
    let result;
    let settled = false;
    const transaction = database.transaction(storeNames, mode);
    transaction.oncomplete = () => {
      settled = true;
      database.close();
      resolve(result);
    };
    transaction.onabort = () => {
      if (!settled) reject(transaction.error || new Error('Transaction IndexedDB annulée.'));
      database.close();
    };
    transaction.onerror = () => {
      if (!settled) reject(transaction.error || new Error('Écriture IndexedDB impossible.'));
    };
    try {
      action(transaction, value => { result = value; });
    } catch (error) {
      transaction.abort();
      reject(error);
    }
  }));
}

export function saveEvent(event) {
  return runTransaction(EVENT_STORE, 'readwrite', transaction => {
    transaction.objectStore(EVENT_STORE).add(event);
  });
}

export function readPendingEvents(limit = 100) {
  return runTransaction(EVENT_STORE, 'readonly', (transaction, setResult) => {
    const request = transaction.objectStore(EVENT_STORE).getAll();
    request.onsuccess = () => {
      setResult(request.result
        .sort((left, right) => left.local_time.localeCompare(right.local_time))
        .slice(0, limit));
    };
  }).then(events => events || []);
}

export function removeAcknowledgedEvents(eventIds) {
  if (!eventIds.length) return Promise.resolve();
  return runTransaction(EVENT_STORE, 'readwrite', transaction => {
    const store = transaction.objectStore(EVENT_STORE);
    eventIds.forEach(eventId => store.delete(eventId));
  });
}

export function hasQueuedToken(token) {
  return runTransaction(EVENT_STORE, 'readonly', (transaction, setResult) => {
    const request = transaction.objectStore(EVENT_STORE).index('token').getKey(token);
    request.onsuccess = () => { setResult(request.result !== undefined); };
  });
}

export function countPendingEvents() {
  return runTransaction(EVENT_STORE, 'readonly', (transaction, setResult) => {
    const request = transaction.objectStore(EVENT_STORE).count();
    request.onsuccess = () => { setResult(request.result); };
  });
}

export function getSetting(key) {
  return runTransaction(SETTINGS_STORE, 'readonly', (transaction, setResult) => {
    const request = transaction.objectStore(SETTINGS_STORE).get(key);
    request.onsuccess = () => { setResult(request.result?.value); };
  });
}

export function setSetting(key, value) {
  return runTransaction(SETTINGS_STORE, 'readwrite', transaction => {
    transaction.objectStore(SETTINGS_STORE).put({ key, value });
  });
}

export function deleteSetting(key) {
  return runTransaction(SETTINGS_STORE, 'readwrite', transaction => {
    transaction.objectStore(SETTINGS_STORE).delete(key);
  });
}

export async function getOrCreateDeviceId() {
  let deviceId = await getSetting('deviceId');
  if (deviceId) return deviceId;
  deviceId = crypto.randomUUID();
  await setSetting('deviceId', deviceId);
  return deviceId;
}

export async function saveDeviceConfiguration(configuration) {
  await setSetting('deviceConfiguration', configuration);
}

export function getDeviceConfiguration() {
  return getSetting('deviceConfiguration');
}
