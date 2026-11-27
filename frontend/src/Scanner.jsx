import { useEffect, useRef, useState } from 'react';
import jsQR from 'jsqr';
import { hasQueuedToken } from './db';

const MAX_CANVAS_WIDTH = 640;
const REPEAT_GUARD_MS = 2000;

export default function Scanner({ onScan, disabled = false }) {
  const videoRef = useRef(null);
  const [active, setActive] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!active) return undefined;
    let cancelled = false;
    let animationFrame = 0;
    let stream;
    let busy = false;
    let lastToken = '';
    let lastReadAt = 0;
    const canvas = document.createElement('canvas');
    const context = canvas.getContext('2d', { willReadFrequently: true });

    async function startCamera() {
      try {
        if (!navigator.mediaDevices?.getUserMedia) {
          throw new Error('unsupported');
        }
        stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: 'environment' },
          audio: false,
        });
        if (cancelled) {
          stream.getTracks().forEach(track => track.stop());
          return;
        }
        videoRef.current.srcObject = stream;
        await videoRef.current.play();
        setError('');
        scanFrame();
      } catch (cameraError) {
        if (cancelled) return;
        if (cameraError.message === 'unsupported') {
          setError('Cette page doit être ouverte en HTTPS ou sur localhost pour utiliser la caméra.');
        } else if (cameraError.name === 'NotAllowedError' || cameraError.name === 'PermissionDeniedError') {
          setError('Accès à la caméra refusé. Autorisez la caméra puis réessayez.');
        } else if (cameraError.name === 'NotFoundError' || cameraError.name === 'DevicesNotFoundError') {
          setError('Aucune caméra n’est disponible sur cet appareil.');
        } else {
          setError('Impossible d’ouvrir la caméra. Vérifiez ses autorisations puis réessayez.');
        }
        setActive(false);
      }
    }

    async function scanFrame() {
      if (cancelled || !videoRef.current || !context || busy) return;
      const video = videoRef.current;
      if (video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && video.videoWidth) {
        const width = Math.min(MAX_CANVAS_WIDTH, video.videoWidth);
        const height = Math.round(video.videoHeight * width / video.videoWidth);
        if (canvas.width !== width || canvas.height !== height) {
          canvas.width = width;
          canvas.height = height;
        }
        context.drawImage(video, 0, 0, width, height);
        const frame = context.getImageData(0, 0, width, height);
        const code = jsQR(frame.data, width, height, { inversionAttempts: 'dontInvert' });
        if (code?.data) {
          const now = performance.now();
          if (code.data !== lastToken || now - lastReadAt >= REPEAT_GUARD_MS) {
            lastToken = code.data;
            lastReadAt = now;
            busy = true;
            try {
              if (!await hasQueuedToken(code.data)) {
                await onScan(code.data);
              }
            } catch (scanError) {
              setError(scanError.message || 'Écriture locale impossible. Réessayez.');
            } finally {
              busy = false;
            }
          }
        }
      }
      if (!cancelled) animationFrame = requestAnimationFrame(scanFrame);
    }

    startCamera();
    return () => {
      cancelled = true;
      cancelAnimationFrame(animationFrame);
      stream?.getTracks().forEach(track => track.stop());
      if (videoRef.current) videoRef.current.srcObject = null;
    };
  }, [active, onScan]);

  return (
    <section className="scanner">
      <button type="button" className="secondary" disabled={disabled} onClick={() => setActive(value => !value)}>
        {active ? 'Fermer la caméra' : 'Démarrer le scan QR'}
      </button>
      {error && <p className="message" role="alert">{error}</p>}
      {active && <video ref={videoRef} className="camera active" muted playsInline />}
    </section>
  );
}
