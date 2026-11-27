let reloadOnControllerChange = false;

export async function registerServiceWorker(onNotice = () => {}) {
  if (!('serviceWorker' in navigator)) {
    onNotice('Ce navigateur ne prend pas en charge le mode hors ligne.');
    return null;
  }
  if (!window.isSecureContext) {
    onNotice('Le mode hors ligne et la caméra exigent HTTPS ou localhost.');
    return null;
  }

  try {
    await navigator.storage?.persist?.();
    const registration = await navigator.serviceWorker.register('/sw.js', {
      scope: '/',
      updateViaCache: 'none',
    });
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      if (reloadOnControllerChange) {
        reloadOnControllerChange = false;
        window.location.reload();
      }
    });
    registration.addEventListener('updatefound', () => {
      const installing = registration.installing;
      installing?.addEventListener('statechange', () => {
        if (installing.state === 'installed' && navigator.serviceWorker.controller) {
          onNotice('Une mise à jour est prête.');
        }
      });
    });
    return registration;
  } catch (error) {
    onNotice(`Le Service Worker n’a pas pu être activé : ${error.message}`);
    return null;
  }
}

export function activateWaitingWorker(registration) {
  if (!registration?.waiting) return false;
  reloadOnControllerChange = true;
  registration.waiting.postMessage({ type: 'SKIP_WAITING' });
  return true;
}
