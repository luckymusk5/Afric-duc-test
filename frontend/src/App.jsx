import React, { useCallback, useEffect, useRef, useState } from 'react';
import { QrSvg } from './qr';

const API = 'http://localhost:8000/api';
const DEVICE_KEY = 'africEducDeviceId';
const DEVICE_ID = localStorage.getItem(DEVICE_KEY) || (() => {
  const id = crypto.randomUUID(); localStorage.setItem(DEVICE_KEY, id); return id;
})();

function b64url(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
}
async function badgeToken(enrollment, seconds) {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'; let bits = '';
  for (const char of enrollment.secret.toUpperCase()) { const value = alphabet.indexOf(char); if (value < 0) throw Error('Invalid enrollment'); bits += value.toString(2).padStart(5, '0'); }
  const secret = new Uint8Array(Math.floor(bits.length / 8));
  for (let i = 0; i < secret.length; i++) secret[i] = parseInt(bits.slice(i * 8, i * 8 + 8), 2);
  const cryptoKey = await crypto.subtle.importKey('raw', secret, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const counter = Math.floor(seconds / 30);
  const digest = await crypto.subtle.sign('HMAC', cryptoKey, new TextEncoder().encode(`${enrollment.badge_id}.${counter}`));
  return `${enrollment.badge_id}.${counter}.${b64url(digest.slice(0, 16))}`;
}

function BadgeScreen() {
  const [enrollment, setEnrollment] = useState(() => { try { return JSON.parse(localStorage.getItem('badgeEnrollment')); } catch { return null; } });
  const [input, setInput] = useState(''); const [token, setToken] = useState(''); const [now, setNow] = useState(Date.now()); const [error, setError] = useState('');
  useEffect(() => { const id = setInterval(() => setNow(Date.now()), 500); return () => clearInterval(id); }, []);
  useEffect(() => { if (!enrollment) return; badgeToken(enrollment, now / 1000).then(setToken).catch(() => setError('Badge invalide.')); }, [enrollment, now]);
  function save(raw) { try { const data = JSON.parse(raw); if (!data.badge_id || !data.secret) throw Error(); localStorage.setItem('badgeEnrollment', JSON.stringify(data)); setEnrollment(data); setInput(''); setError('Badge enrôlé sur cet appareil.'); } catch { setError('QR d’enrôlement invalide.'); } }
  return <main><header><div className="brand">AE <span>AFRIC’ÉDUC · BADGE</span></div><a href="/">Retour à l’application</a></header><section className="hero"><div><p className="eyebrow">BADGE ÉLÈVE</p><h1>Mon QR de passage</h1><p>Le code change automatiquement toutes les 30 secondes.</p></div><div className="date">{Math.ceil((30 - (now / 1000 % 30)) % 30)} s</div></section>
    {!enrollment ? <section className="card panel"><h2>Enrôler ce badge</h2><p>Scanne le QR remis par l’école ou colle son contenu ici. Le secret est gardé uniquement sur cet appareil.</p><CameraReader onRead={setInput}/><textarea value={input} onChange={e => setInput(e.target.value)} placeholder="Contenu du QR d’enrôlement"/><button onClick={() => save(input)}>Enregistrer le badge</button></section> : <section className="card panel badge-card"><QrSvg value={token} size={280}/><p>Présente ce QR à l’agent. Il ne contient ni nom ni numéro d’élève.</p><small>Se renouvelle dans {Math.ceil((30 - (now / 1000 % 30)) % 30)} secondes</small><button className="secondary" onClick={() => { localStorage.removeItem('badgeEnrollment'); setEnrollment(null); setToken(''); }}>Retirer le badge de cet appareil</button></section>}{error && <p className="notice">{error}</p>}</main>;
}

export default function App() {
  return location.pathname === '/badge' ? <BadgeScreen/> : <AppShell/>;
}

function AppShell() {
  const [token, setToken] = useState(localStorage.getItem('access') || '');
  const [user, setUser] = useState(null); const [queue, setQueue] = useState(() => JSON.parse(localStorage.getItem('scanQueue') || '[]'));
  const [pending, setPending] = useState(() => JSON.parse(localStorage.getItem('pendingScans') || '[]'));
  const [message, setMessage] = useState(''); const [students, setStudents] = useState([]); const [badges, setBadges] = useState([]); const [enrollment, setEnrollment] = useState(null);
  const [checkpoints, setCheckpoints] = useState([]); const [checkpoint, setCheckpoint] = useState(''); const [summary, setSummary] = useState(null);
  const [scanToken, setScanToken] = useState(''); const [clock, setClock] = useState(null);

  const call = useCallback(async (path, options = {}) => {
    let access = token;
    const send = value => fetch(API + path, { ...options, headers: { 'Content-Type': 'application/json', ...(value ? { Authorization: `Bearer ${value}` } : {}), ...(options.headers || {}) } });
    let response = await send(access);
    if (response.status === 401 && access && !path.includes('/auth/')) {
      const refresh = localStorage.getItem('refresh');
      if (refresh) { const renewed = await fetch(API + '/auth/refresh/', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ refresh }) }); if (renewed.ok) { const auth = await renewed.json(); access = auth.access; localStorage.setItem('access', auth.access); if (auth.refresh) localStorage.setItem('refresh', auth.refresh); setToken(auth.access); response = await send(access); } }
    }
    const data = response.status === 204 ? {} : await response.json();
    if (!response.ok) throw Error(data.detail || 'Une erreur est survenue');
    return data;
  }, [token]);

  useEffect(() => { localStorage.setItem('scanQueue', JSON.stringify(queue)); }, [queue]);
  useEffect(() => { localStorage.setItem('pendingScans', JSON.stringify(pending)); }, [pending]);
  useEffect(() => { if (!token) return; call('/me/').then(setUser).catch(logout); }, [token]);
  useEffect(() => { if (!user) return; if (user.role === 'agent' || user.role === 'admin') { call('/checkpoints/').then(items => { setCheckpoints(items); if (items.length) setCheckpoint(String(items[0].id)); }).catch(() => {}); } if (user.role === 'admin') { call('/students/').then(setStudents).catch(() => {}); call('/badges/').then(setBadges).catch(() => {}); } }, [user?.role]);
  useEffect(() => { if (!token || !pending.length) return; let active = true; const poll = async () => { try { const data = await call('/scans/sync/status/?eventIds=' + pending.join(',')); if (!active) return; const done = data.items.filter(x => ['processed', 'rejected', 'failed'].includes(x.state)); if (done.length) { setPending(old => old.filter(id => !done.some(x => x.eventId === id))); setMessage(done.some(x => x.state !== 'processed') ? 'Certains passages ont été refusés. Vérifiez l’heure ou le badge.' : `${done.length} passage(s) enregistré(s) dans PostgreSQL.`); } } catch {} }; poll(); const id = setInterval(poll, 2500); return () => { active = false; clearInterval(id); }; }, [token, pending.join(',')]);

  async function logout() { try { if (token) await fetch(API + '/auth/logout/', { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, body: JSON.stringify({ refresh: localStorage.getItem('refresh') }) }); } finally { localStorage.removeItem('access'); localStorage.removeItem('refresh'); setToken(''); setUser(null); } }
  async function login(e) { e.preventDefault(); const fd = new FormData(e.currentTarget); try { const data = await call('/auth/login/', { method: 'POST', body: JSON.stringify({ username: fd.get('email'), password: fd.get('password') }) }); localStorage.setItem('access', data.access); localStorage.setItem('refresh', data.refresh); setToken(data.access); } catch (err) { setMessage(err.message); } }
  async function calibrate() {
    const localTime = new Date(); const started = performance.now();
    const data = await call('/devices/time-sync/', { method: 'POST', body: JSON.stringify({ deviceId: DEVICE_ID, localTime: localTime.toISOString() }) });
    const elapsed = (performance.now() - started) / 2; const serverEpoch = Date.parse(data.serverTime) + elapsed;
    const anchor = { serverEpoch, perf: performance.now(), offset: data.offsetSeconds }; setClock(anchor); return anchor;
  }
  async function addScan(e) { e.preventDefault(); const anchor = clock || await calibrate(); const seconds = (anchor.serverEpoch + performance.now() - anchor.perf) / 1000; const localTime = new Date().toISOString(); const estimatedTime = new Date(seconds * 1000).toISOString(); const scan = { eventId: crypto.randomUUID(), token: scanToken.trim(), checkpointId: Number(checkpoint), localTime, estimatedTime, deviceId: DEVICE_ID, clockOffsetSeconds: anchor.offset }; setQueue(old => [...old, scan]); setScanToken(''); setMessage('Passage conservé sur cet appareil, prêt à être synchronisé.'); }
  async function sync() { try { if (!clock) await calibrate(); const result = await call('/scans/sync/', { method: 'POST', body: JSON.stringify({ scans: queue }) }); const accepted = new Set(result.queued.map(item => item.eventId)); setQueue(old => old.filter(scan => !accepted.has(scan.eventId))); setPending(old => [...new Set([...old, ...result.queued.filter(item => item.state !== 'processed').map(item => item.eventId)])]); setMessage(`${result.queued.length} passage(s) remis à RabbitMQ; ${result.errors.length} refus.`); } catch { setMessage('Réseau indisponible : les passages restent dans la file locale.'); } }
  async function issueBadge(student) { try { const result = await call(`/students/${student.id}/badges/reissue/`, { method: 'POST', body: '{}' }); setEnrollment({ student, ...result }); call('/badges/').then(setBadges); } catch (err) { setMessage(err.message); } }
  async function changeBadgeState(badge, state) { try { const updated = await call(`/badges/${badge.badge_id}/state/`, { method: 'PATCH', body: JSON.stringify({ state }) }); setBadges(old => old.map(item => item.badge_id === badge.badge_id ? updated : item)); } catch (err) { setMessage(err.message); } }
  async function loadSummary() { try { setSummary(await call('/summaries/daily/?class=6e%20A')); } catch (err) { setMessage(err.message); } }

  if (!token) return <main className="login"><section className="card"><div className="brand">AE <span>AFRIC’ÉDUC</span></div><p className="eyebrow">SCHOOLLOGS · PASSAGE</p><h1>Suivi des passages</h1><p>Connectez-vous à votre espace sécurisé.</p><form onSubmit={login}><label>Email<input name="email" type="email" required placeholder="nom@ecole.cm"/></label><label>Mot de passe<input name="password" type="password" required/></label><button>Se connecter <b>→</b></button></form>{message && <p className="message">{message}</p>}<small>Prototype local · Afric’Éduc</small><p><a href="/badge">Ouvrir mon badge élève</a></p></section></main>;
  return <main><header><div className="brand">AE <span>AFRIC’ÉDUC</span></div><div className="account"><span>{user?.name} · {user?.role}</span><button className="quiet" onClick={logout}>Déconnexion</button></div></header>
    <section className="hero"><div><p className="eyebrow">SCHOOLLOGS · AUJOURD’HUI</p><h1>Bonjour, {user?.name?.split(' ')[0]}</h1><p>Les passages restent disponibles même quand le réseau ralentit.</p></div><div className="date">{new Intl.DateTimeFormat('fr-FR', { weekday: 'long', day: 'numeric', month: 'long' }).format(new Date())}</div></section>{message && <div className="notice">{message}</div>}
    {user?.role === 'agent' && <section className="card panel"><div className="panel-head"><div><p className="eyebrow">PORTAIL</p><h2>Enregistrer un passage</h2></div><span className="status">● {navigator.onLine ? 'Connecté' : 'Hors ligne'}</span></div><p>L’heure utilisée est estimée par horloge monotone après calibration réseau.</p><button className="secondary" onClick={() => calibrate().then(() => setMessage('Horloge du portail synchronisée.')).catch(() => setMessage('Calibration indisponible.'))}>Calibrer l’horloge</button><form onSubmit={addScan}><label>Code QR tournant<input value={scanToken} onChange={e => setScanToken(e.target.value)} required placeholder="Scannez ou saisissez le jeton"/></label><CameraReader onRead={setScanToken}/><label>Point de contrôle<select value={checkpoint} onChange={e => setCheckpoint(e.target.value)}>{checkpoints.map(point => <option key={point.id} value={point.id}>{point.label}</option>)}</select></label><button disabled={!checkpoint}>Ajouter à la file <b>＋</b></button></form><div className="queue"><span>Sur cet appareil</span><strong>{queue.length}</strong><span>En traitement serveur</span><strong>{pending.length}</strong><button className="secondary" disabled={!queue.length || !navigator.onLine} onClick={sync}>Synchroniser</button></div></section>}
    {user?.role === 'admin' && <><section className="grid"><article className="card panel"><p className="eyebrow">SYNTHÈSE</p><h2>6e A · aujourd’hui</h2><button onClick={loadSummary}>Générer la synthèse</button>{summary && <div className="summary"><span>{summary.origin === 'model' ? 'Synthèse IA' : 'Gabarit local'}</span><p>{summary.text}</p></div>}</article><article className="card panel"><p className="eyebrow">SYNCHRONISATION</p><h2>File du portail</h2><div className="big-number">{queue.length + pending.length}</div><p>{queue.length} local · {pending.length} dans RabbitMQ</p></article></section><section className="card panel"><p className="eyebrow">BADGES</p><h2>Émettre ou remplacer un badge</h2>{students.map(student => <div className="student-row" key={student.id}><span>{student.name} · {student.class_name}</span><button className="secondary" onClick={() => issueBadge(student)}>Émettre / remplacer</button></div>)}{enrollment && <div className="enrollment"><h3>QR d’enrôlement — {enrollment.student.name}</h3><QrSvg value={JSON.stringify(enrollment.enrollment)} size={220}/><p>Affichez ce QR une seule fois pour l’enrôlement. Fermez cette fenêtre après remise.</p><button className="secondary" onClick={() => setEnrollment(null)}>Fermer le QR</button></div>}<h3>État des badges</h3>{badges.map(badge => <div className="student-row" key={badge.badge_id}><span>{students.find(student => student.id === badge.student_id)?.name || 'Élève'} · {badge.state}</span><div>{['suspended', 'lost', 'revoked'].map(state => <button key={state} className="secondary" disabled={badge.state === 'revoked' || badge.state === state} onClick={() => changeBadgeState(badge, state)}>{state === 'suspended' ? 'Suspendre' : state === 'lost' ? 'Signaler perdu' : 'Révoquer'}</button>)}</div></div>)}</section></>}
    {user?.role === 'parent' && <section className="card panel"><p className="eyebrow">ESPACE FAMILLE</p><h2>Vos enfants</h2><ParentView call={call}/></section>}
    <footer>AFRIC’ÉDUC <span>•</span> Passages sécurisés, même sans réseau.</footer></main>;
}

function ParentView({ call }) { const [items, setItems] = useState([]); const [scans, setScans] = useState([]); useEffect(() => { Promise.all([call('/anomalies/'), call('/scans/')]).then(([a, s]) => { setItems(a); setScans(s); }).catch(() => {}); }, []); return <><h3>Passages récents</h3>{scans.length ? scans.slice(0, 10).map(scan => <p key={scan.id} className="notice">{scan.checkpoint} · {new Date(scan.estimated_time || scan.received_at).toLocaleString('fr-FR')}{scan.clock_suspect ? ' · heure à vérifier' : ''}</p>) : <p>Aucun passage à afficher.</p>}<h3>Anomalies</h3>{items.length ? items.map(item => <p key={item.id} className="notice">{item.student.name} · {item.details || item.kind}</p>) : <p>Aucune anomalie à afficher.</p>}</>; }

function CameraReader({ onRead }) {
  const video = useRef(null); const stream = useRef(null); const [active, setActive] = useState(false);
  useEffect(() => {
    if (!active) return;
    let stopped = false; let frame;
    (async () => {
      try {
        if (!('BarcodeDetector' in window)) throw Error();
        stream.current = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } });
        if (stopped) return;
        video.current.srcObject = stream.current; await video.current.play();
        const detector = new BarcodeDetector({ formats: ['qr_code'] });
        const scan = async () => {
          if (stopped || !video.current) return;
          try { const codes = await detector.detect(video.current); if (codes.length) { onRead(codes[0].rawValue); setActive(false); return; } } catch {}
          frame = requestAnimationFrame(scan);
        };
        scan();
      } catch { onRead(''); setActive(false); }
    })();
    return () => { stopped = true; cancelAnimationFrame(frame); stream.current?.getTracks().forEach(track => track.stop()); stream.current = null; };
  }, [active]);
  return <><button type="button" className="secondary" onClick={() => setActive(value => !value)}>{active ? 'Fermer la caméra' : 'Scanner un QR avec la caméra'}</button><video ref={video} className={active ? 'camera active' : 'camera'} muted playsInline/></>;
}
