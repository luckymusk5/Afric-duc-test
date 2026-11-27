import React, { useCallback, useEffect, useState } from 'react';
import { QrSvg } from './qr';
import Scanner from './Scanner';
import {
  countPendingEvents,
  deleteSetting,
  getDeviceConfiguration,
  getOrCreateDeviceId,
  hasQueuedToken,
  saveDeviceConfiguration,
  saveEvent,
} from './db';
import { estimatedServerTime, restoreClock, saveClockCheckpoint, updateServerTime } from './clock';
import { queueAndSend, startSynchronization, synchronizePending } from './sync';
import { activateWaitingWorker, registerServiceWorker } from './register-sw';

const API = import.meta.env.VITE_API_URL || 'http://localhost:8000/api';
const QR_STEP_SECONDS = 15;

async function refreshCsrfToken() {
  const response = await fetch(`${API}/auth/csrf/`, { credentials: 'include' });
  if (!response.ok) throw new Error('Impossible de sécuriser la session.');
  const { csrfToken } = await response.json();
  if (!csrfToken) throw new Error('Le serveur n’a pas fourni de jeton CSRF.');
  localStorage.setItem('schoolLogsCsrf', csrfToken);
  return csrfToken;
}

function readLocalList(key) {
  try { const value = JSON.parse(localStorage.getItem(key) || '[]'); return Array.isArray(value) ? value : []; }
  catch { return []; }
}
export default function App() {
  return <AppShell/>;
}

function AppShell() {
  const [token, setToken] = useState(localStorage.getItem('access') || '');
  const [user, setUser] = useState(() => { try { return JSON.parse(localStorage.getItem('schoolLogsUser')); } catch { return null; } });
  const [queue, setQueue] = useState(0);
  const [message, setMessage] = useState(''); const [students, setStudents] = useState([]); const [badges, setBadges] = useState([]); const [parents, setParents] = useState([]); const [agents, setAgents] = useState([]);
  const [checkpoints, setCheckpoints] = useState(() => readLocalList('scanCheckpoints')); const [checkpoint, setCheckpoint] = useState(() => { const first = readLocalList('scanCheckpoints')[0]; return first ? String(first.id) : ''; }); const [summary, setSummary] = useState(null);
  const [deviceConfiguration, setDeviceConfiguration] = useState(null);
  const [nonTreatedAnomalies, setNonTreatedAnomalies] = useState(0);
  const [agentNotice, setAgentNotice] = useState('');
  const [clockReady, setClockReady] = useState(false);
  const [networkOnline, setNetworkOnline] = useState(navigator.onLine);
  const [serviceWorkerRegistration, setServiceWorkerRegistration] = useState(null);
  const [updateReady, setUpdateReady] = useState(false);
  const [studentBadge, setStudentBadge] = useState(null); const [studentQr, setStudentQr] = useState(''); const [qrNow, setQrNow] = useState(performance.now());

  const call = useCallback(async (path, options = {}) => {
    let access = token;
    const method = (options.method || 'GET').toUpperCase();
    const csrfToken = ['POST', 'PATCH', 'PUT', 'DELETE'].includes(method)
      ? await refreshCsrfToken()
      : '';
    const send = value => fetch(API + path, {
      ...options,
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        ...(value ? { Authorization: `Bearer ${value}` } : {}),
        ...(csrfToken ? { 'X-CSRFToken': csrfToken } : {}),
        ...(options.headers || {}),
      },
    });
    let response = await send(access);
    if (response.status === 401 && access && !path.includes('/auth/')) {
      const refresh = localStorage.getItem('refresh');
      if (refresh) { const renewed = await fetch(API + '/auth/refresh/', { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ refresh }) }); if (renewed.ok) { const auth = await renewed.json(); access = auth.access; localStorage.setItem('access', auth.access); if (auth.refresh) localStorage.setItem('refresh', auth.refresh); setToken(auth.access); response = await send(access); } }
    }
    const data = response.status === 204 ? {} : await response.json();
    if (!response.ok) { const error = Error(data.detail || 'Une erreur est survenue'); error.status = response.status; throw error; }
    return data;
  }, [token]);

  useEffect(() => {
    let active = true;
    registerServiceWorker(notice => {
      if (!active) return;
      if (notice.includes('mise à jour')) {
        setUpdateReady(true);
      } else {
        setMessage(notice);
      }
    }).then(registration => {
      if (!active || !registration) return;
      setServiceWorkerRegistration(registration);
      setUpdateReady(Boolean(registration.waiting));
    });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    const handleOtherTabLogout = event => {
      if (event.key !== 'access' || event.newValue !== null) return;
      setToken(''); setUser(null); setStudentBadge(null); setStudentQr('');
    };
    window.addEventListener('storage', handleOtherTabLogout);
    return () => window.removeEventListener('storage', handleOtherTabLogout);
  }, []);

  useEffect(() => {
    let active = true;
    getDeviceConfiguration()
      .then(configuration => { if (active) setDeviceConfiguration(configuration); })
      .catch(error => { if (active) setMessage(error.message); });
    return () => { active = false; };
  }, []);

  function updateApplication() {
    if (!activateWaitingWorker(serviceWorkerRegistration)) {
      setUpdateReady(false);
    }
  }

  useEffect(() => {
    if (!token) return;
    let active = true;
    call('/me/').then(profile => {
      if (!active) return;
      localStorage.setItem('schoolLogsUser', JSON.stringify(profile)); setUser(profile);
    }).catch(err => {
      if (!active) return;
      if (err.status === 401 && navigator.onLine) { logout(); return; }
      try {
        const cached = JSON.parse(localStorage.getItem('schoolLogsUser'));
        if (cached) setUser(cached);
        else setMessage(err.message);
      } catch {
        setMessage('Le profil local est illisible. Reconnectez-vous pour le recharger.');
      }
    });
    return () => { active = false; };
  }, [token]);
  useEffect(() => {
    if (user?.role !== 'student') { setStudentBadge(null); setStudentQr(''); return; }
    let active = true;
    let refreshTimer = null;
    const loadCredential = async () => {
      if (refreshTimer !== null) clearTimeout(refreshTimer);
      try {
        const requestStartedAt = performance.now();
        const credential = await call('/my/badge/');
        await updateServerTime(credential.server_time, requestStartedAt);
        if (!active) return;
        setStudentBadge({ badge_id: credential.badge_id, expires_at: credential.expires_at });
        setStudentQr(credential.token);
        const untilExpiry = credential.expires_at * 1000 - estimatedServerTime().getTime();
        refreshTimer = setTimeout(loadCredential, Math.max(250, untilExpiry + 100));
      } catch (error) {
        if (!active) return;
        setStudentBadge(null);
        setStudentQr('');
        setMessage(error.message || 'Impossible de générer le QR de cet élève.');
        refreshTimer = setTimeout(loadCredential, 3000);
      }
    };
    // Remove the old client-side badge secret from IndexedDB.
    deleteSetting('studentBadgeCredential').catch(() => {});
    loadCredential();
    const refreshWhenOnline = () => { loadCredential(); };
    window.addEventListener('online', refreshWhenOnline);
    return () => {
      active = false;
      if (refreshTimer !== null) clearTimeout(refreshTimer);
      window.removeEventListener('online', refreshWhenOnline);
    };
  }, [user?.role, token, call]);
  useEffect(() => {
    if (user?.role !== 'student') return;
    const id = setInterval(() => setQrNow(performance.now()), 500);
    return () => clearInterval(id);
  }, [user?.role]);
  useEffect(() => {
    if (!user) return;
    if (user.role === 'agent' || user.role === 'admin') {
      call('/checkpoints/')
        .then(items => {
          localStorage.setItem('scanCheckpoints', JSON.stringify(items));
          setCheckpoints(items);
          if (items.length) setCheckpoint(String(items[0].id));
        })
        .catch(error => {
          const cached = readLocalList('scanCheckpoints');
          setCheckpoints(cached);
          if (cached.length) setCheckpoint(String(cached[0].id));
          else setMessage(error.message);
        });
    }
    if (user.role === 'admin') {
      call('/students/').then(setStudents).catch(error => setMessage(error.message));
      call('/badges/').then(setBadges).catch(error => setMessage(error.message));
      call('/parents/').then(setParents).catch(error => setMessage(error.message));
      call('/agents/').then(setAgents).catch(error => setMessage(error.message));
    }
  }, [user?.role, token, call]);
  useEffect(() => {
    if (user?.role !== 'admin') return;
    call('/anomalies/non-treated-count/')
      .then(data => setNonTreatedAnomalies(data.non_treated))
      .catch(error => setMessage(error.message));
  }, [user?.role, token, call]);
  const refreshQueueCount = useCallback(async () => {
    setQueue(await countPendingEvents());
  }, []);

  async function logout() {
    let serverLogoutConfirmed = true;
    try {
      const refresh = localStorage.getItem('refresh');
      const csrfToken = await refreshCsrfToken();
      const response = await fetch(API + '/auth/logout/', {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRFToken': csrfToken,
        },
        body: JSON.stringify({ access: token, refresh }),
      });
      serverLogoutConfirmed = response.ok;
    } catch {
      serverLogoutConfirmed = false;
    } finally {
      localStorage.removeItem('access');
      localStorage.removeItem('refresh');
      localStorage.removeItem('schoolLogsUser');
      localStorage.removeItem('schoolLogsCsrf');
      setToken('');
      setUser(null);
      setStudentBadge(null);
      setStudentQr('');
      if (!serverLogoutConfirmed) setMessage('Déconnexion locale effectuée, mais le serveur n’a pas confirmé la révocation des jetons. Reconnectez-vous en ligne pour la confirmer.');
    }
  }

  async function login(e) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    try {
      const credentials = { username: form.get('identifier'), password: form.get('password') };
      const auth = await call('/auth/login/', {
        method: 'POST',
        body: JSON.stringify(credentials),
      });
      const csrfToken = await refreshCsrfToken();
      const sessionResponse = await fetch(`${API}/auth/session/`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify(credentials),
      });
      if (!sessionResponse.ok) throw new Error('Connexion refusée.');
      // django_login rotates the CSRF secret; discard the pre-login token.
      await refreshCsrfToken();
      localStorage.setItem('access', auth.access);
      localStorage.setItem('refresh', auth.refresh);
      setToken(auth.access);
    } catch (err) {
      setMessage(err.message);
    }
  }

  async function calibratePortal(configuration) {
    const localTime = new Date().toISOString();
    const started = performance.now();
    const csrfToken = await refreshCsrfToken();
    const response = await fetch(`${API}/devices/time-sync/`, {
      method: 'POST',
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': csrfToken,
        'X-Device-Token': configuration.device_token,
      },
      body: JSON.stringify({ local_time: localTime }),
    });
    if (!response.ok) throw new Error('Calibration du portail impossible.');
    const data = await response.json();
    await updateServerTime(data.serverTime, started);
    setClockReady(true);
  }

  const handleScan = useCallback(async scannedToken => {
    if (!deviceConfiguration) throw new Error('Ce portail doit être enrôlé par un responsable.');
    if (!clockReady) throw new Error('Connectez le portail pour synchroniser son horloge.');
    if (await hasQueuedToken(scannedToken)) return;
    const estimatedTime = estimatedServerTime();
    const event = {
      event_id: crypto.randomUUID(),
      device_id: deviceConfiguration.device_id,
      token: scannedToken,
      local_time: new Date().toISOString(),
      est_time: estimatedTime.toISOString(),
    };
    await saveEvent(event);
    await saveClockCheckpoint(estimatedTime).catch(() => {});
    await refreshQueueCount();
    setAgentNotice('Scan lu');
    await queueAndSend(event, async result => {
      setAgentNotice(result === 'refuse'
        ? 'REFUSÉ. Vérifiez que l’élève est déjà entré et que son QR est valide.'
        : 'Passage confirmé.');
      await refreshQueueCount();
    });
  }, [deviceConfiguration, clockReady, refreshQueueCount]);

  useEffect(() => {
    if (user?.role !== 'agent') return undefined;
    let active = true;
    let configuration;
    const updateNetwork = () => setNetworkOnline(navigator.onLine);
    const initialize = async () => {
      try {
        configuration = await getDeviceConfiguration();
        const restoredClock = await restoreClock();
        if (!active) return;
        setDeviceConfiguration(configuration);
        setClockReady(restoredClock);
        await refreshQueueCount();
        if (configuration && navigator.onLine) {
          await calibratePortal(configuration);
        }
      } catch (error) {
        if (active) setAgentNotice(error.message || 'Portail indisponible. Réessayez.');
      }
    };
    const reconnect = () => {
      updateNetwork();
      if (configuration) {
        calibratePortal(configuration).catch(error => {
          setAgentNotice(error.message);
        });
      }
    };
    window.addEventListener('online', reconnect);
    window.addEventListener('offline', updateNetwork);
    initialize();
    return () => {
      active = false;
      window.removeEventListener('online', reconnect);
      window.removeEventListener('offline', updateNetwork);
    };
  }, [user?.role, refreshQueueCount]);

  useEffect(() => {
    if (user?.role !== 'agent') return undefined;
    return startSynchronization({
      onQueueCount: refreshQueueCount,
      onAuthenticationRequired: notice => setAgentNotice(notice),
    });
  }, [user?.role, refreshQueueCount]);

  async function enrollCurrentDevice() {
    try {
      if (!checkpoint) throw new Error('Créez ou sélectionnez un point de contrôle.');
      const deviceId = await getOrCreateDeviceId();
      const current = await getDeviceConfiguration();
      let configuration;
      if (current) {
        await call(`/devices/${current.device_id}/assign/`, {
          method: 'POST',
          body: JSON.stringify({ checkpoint_id: Number(checkpoint) }),
        });
        configuration = { ...current, checkpoint: checkpoints.find(item => String(item.id) === checkpoint)?.label || '' };
      } else {
        const enrolled = await call('/devices/', {
          method: 'POST',
          body: JSON.stringify({ device_id: deviceId, checkpoint_id: Number(checkpoint) }),
        });
        configuration = {
          device_id: enrolled.device_id,
          device_token: enrolled.device_token,
          checkpoint: enrolled.checkpoint,
        };
      }
      await saveDeviceConfiguration(configuration);
      setDeviceConfiguration(configuration);
      setMessage('Portail enrôlé. Le secret a été conservé sur cet appareil.');
    } catch (err) {
      setMessage(err.message);
    }
  }
  async function issueBadge(student) { try { await call(`/students/${student.id}/badges/reissue/`, { method: 'POST', body: '{}' }); setBadges(await call('/badges/')); setMessage(`Badge remplacé pour ${student.name}.`); } catch (err) { setMessage(err.message); } }
  async function changeBadgeState(badge, state) { try { const updated = await call(`/badges/${badge.badge_id}/state/`, { method: 'PATCH', body: JSON.stringify({ state }) }); setBadges(old => old.map(item => item.badge_id === badge.badge_id ? updated : item)); } catch (err) { setMessage(err.message); } }
  async function loadSummary() { try { setSummary(await call('/analytics/school/')); } catch (err) { setMessage(err.message); } }

  if (!token) return <main className="login"><section className="card"><div className="brand">AE <span>AFRIC’ÉDUC</span></div><p className="eyebrow">SCHOOLLOGS · PASSAGE</p><h1>Suivi des passages</h1><p>Connectez-vous à votre espace sécurisé.</p><form onSubmit={login}><label>Identifiant<input name="identifier" type="text" autoComplete="username" required placeholder="Ex. 20260001"/></label><label>Mot de passe<input name="password" type="password" autoComplete="current-password" required/></label><button>Se connecter <b>→</b></button></form>{message && <p className="message">{message}</p>}<small>Prototype local · Afric’Éduc</small><p><a href="/eleve">Connexion élève</a></p></section></main>;
  if (user?.role === 'agent') {
    return (
      <main className="scan-screen">
        <header>
          <div className="brand">AFRIC’ÉDUC <span>{deviceConfiguration?.checkpoint || 'Portail'}</span></div>
          {updateReady && <button className="secondary" onClick={updateApplication}>Mettre à jour</button>}
          <button className="quiet" onClick={logout}>Déconnexion</button>
        </header>
        <section className="card panel scan-panel">
          <h1>Scanner un badge</h1>
          <div className="scan-status">
            <span className="status">● {networkOnline ? 'En ligne' : 'Hors ligne'}</span>
            <span>En attente : <strong>{queue}</strong></span>
            <strong>{deviceConfiguration && clockReady ? 'Prêt hors ligne' : 'Configuration requise'}</strong>
          </div>
          {agentNotice && <p className="notice" role="status">{agentNotice}</p>}
          {!deviceConfiguration && <p className="notice">Ce portail doit être enrôlé par un responsable.</p>}
          {deviceConfiguration && !clockReady && <p className="notice">Synchronisez l’horloge avant le premier scan.</p>}
          <Scanner onScan={handleScan} disabled={!deviceConfiguration || !clockReady} />
          <button className="secondary" onClick={synchronizePending}>Synchroniser maintenant</button>
        </section>
        <footer>AFRIC’ÉDUC · Portail de passage</footer>
      </main>
    );
  }
  return <main><header><div className="brand">AE <span>AFRIC’ÉDUC</span></div><div className="account"><span>{user?.name} · {user?.role}</span>{updateReady && <button className="secondary" onClick={updateApplication}>Mettre à jour</button>}<button className="quiet" onClick={logout}>Déconnexion</button></div></header>
    <section className="hero"><div><p className="eyebrow">SCHOOLLOGS · AUJOURD’HUI</p><h1>Bonjour, {user?.name?.split(' ')[0]}</h1><p>Les passages restent disponibles même quand le réseau ralentit.</p></div><div className="date">{new Intl.DateTimeFormat('fr-FR', { weekday: 'long', day: 'numeric', month: 'long' }).format(new Date())}</div></section>{message && <div className="notice">{message}</div>}{user?.role === 'admin' && <div className="notice">Anomalies non traitées : <strong>{nonTreatedAnomalies}</strong></div>}
    {user?.role === 'admin' && <><section className="grid"><article className="card panel"><p className="eyebrow">ANALYSES</p><h2>Présences et retards</h2><button onClick={loadSummary}>Générer la synthèse</button></article><article className="card panel"><p className="eyebrow">SYNCHRONISATION</p><h2>File locale du portail</h2><div className="big-number">{queue}</div><p>Événements en attente sur cet appareil.</p></article></section>{summary && <AdminAnalytics call={call} initial={summary}/>}<AdminWorkspace call={call} students={students} setStudents={setStudents} badges={badges} setBadges={setBadges} parents={parents} setParents={setParents} agents={agents} setAgents={setAgents} setMessage={setMessage} issueBadge={issueBadge} changeBadgeState={changeBadgeState}/></>}
    {user?.role === 'student' && <section className="card panel badge-card"><p className="eyebrow">VOTRE PASS PERSONNEL</p><h2>Bonjour {user.name}</h2>{studentBadge ? <>{studentQr ? <QrSvg value={studentQr} size={280}/> : <p>Génération du QR…</p>}<p>Présentez ce QR à l’agent au point de contrôle. Votre code personnel se renouvelle automatiquement.</p><small>Prochain renouvellement dans {Math.ceil((QR_STEP_SECONDS - (estimatedServerTime().getTime() / 1000 % QR_STEP_SECONDS)) % QR_STEP_SECONDS)} secondes</small></> : <p>Chargement du QR élève…</p>}</section>}
    {user?.role === 'parent' && <section className="card panel"><p className="eyebrow">ESPACE FAMILLE</p><h2>Vos enfants</h2><ParentView call={call}/></section>}
    <footer>AFRIC’ÉDUC <span>•</span> Passages sécurisés, même sans réseau.</footer></main>;
}

function AdminAnalytics({ call, initial }) {
  const [overview, setOverview] = useState(initial);
  const [dateValue, setDateValue] = useState(initial.date);
  const [className, setClassName] = useState(initial.classes[0]?.class_name || '');
  const [classReport, setClassReport] = useState(null);
  const [selectedStudent, setSelectedStudent] = useState(null);
  const [studentHistory, setStudentHistory] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => { setOverview(initial); }, [initial]);
  useEffect(() => {
    let active = true;
    if (!className) { setClassReport(null); return undefined; }
    call(`/analytics/class/?class_name=${encodeURIComponent(className)}&date=${encodeURIComponent(dateValue)}`)
      .then(data => { if (active) { setClassReport(data); setError(''); } })
      .catch(requestError => { if (active) setError(requestError.message); });
    return () => { active = false; };
  }, [call, className, dateValue]);
  useEffect(() => {
    let active = true;
    if (!selectedStudent) { setStudentHistory(null); return undefined; }
    call(`/analytics/students/${selectedStudent.id}/history/?date=${encodeURIComponent(dateValue)}`)
      .then(data => { if (active) setStudentHistory(data); })
      .catch(requestError => { if (active) setError(requestError.message); });
    return () => { active = false; };
  }, [call, selectedStudent, dateValue]);

  async function changeDate(nextDate) {
    setDateValue(nextDate);
    setSelectedStudent(null);
    try {
      const data = await call(`/analytics/school/?date=${encodeURIComponent(nextDate)}`);
      setOverview(data);
      if (!data.classes.some(group => group.class_name === className)) setClassName(data.classes[0]?.class_name || '');
    } catch (requestError) { setError(requestError.message); }
  }

  const studentsByStatus = status => classReport?.students.filter(student => student.status === status) || [];
  const openStudent = student => setSelectedStudent(student);
  const statusLabel = { present: 'Présent', exited: 'Sorti', absent: 'Absent' };
  return <div className="admin-analytics">
    <div className="analytics-controls"><label>Date d’analyse<input type="date" value={dateValue} onChange={event => changeDate(event.target.value)}/></label><label>Classe<select value={className} onChange={event => { setClassName(event.target.value); setSelectedStudent(null); }}>{overview.classes.map(group => <option key={group.class_name} value={group.class_name}>{group.class_name}</option>)}</select></label></div>
    {error && <p className="message" role="alert">{error}</p>}
    {!overview.classes.length ? <p>Aucune classe active n’est enregistrée.</p> : <div className="attendance-table-wrap"><table className="attendance-table"><thead><tr><th>Classe</th><th>Effectif</th><th>Présents</th><th>Sortis</th><th>Absents</th><th>Alertes retards</th></tr></thead><tbody>{overview.classes.map(group => <tr key={group.class_name}><td><button className="table-link" onClick={() => setClassName(group.class_name)}>{group.class_name}</button></td><td>{group.total}</td><td>{group.present}</td><td>{group.exited}</td><td>{group.absent}</td><td>{group.late_students}</td></tr>)}</tbody></table></div>}
    {!className ? <p>Sélectionnez une classe pour afficher les passages.</p> : !classReport ? <p>Analyse des passages…</p> : <>
      <div className="attendance-totals analytics-totals">{[['Effectif', classReport.totals.total], ['Présents', classReport.totals.present], ['Sortis', classReport.totals.exited], ['Absents', classReport.totals.absent], ['Entrés', classReport.totals.entered]].map(([label, value]) => <article className="attendance-total" key={label}><small>{label}</small><strong>{value}</strong></article>)}</div>
      <p className="summary">{classReport.totals.present} élève(s) présent(s), {classReport.totals.exited} sorti(s) et {classReport.totals.absent} absent(s) sur {classReport.totals.total} en {classReport.class_name}.</p>
      <h3>Entrées et sorties par heure</h3><HourlyPassageChart bins={classReport.hourly}/>
      <div className="class-rosters">
        {[['present', 'Présents dans l’établissement'], ['exited', 'Entrés puis sortis'], ['absent', 'Absents']].map(([key, title]) => <section className="roster-card" key={key}><h4>{title} <span>{studentsByStatus(key).length}</span></h4>{studentsByStatus(key).length ? studentsByStatus(key).map(student => <div className="roster-row" key={student.id}><button className="student-link" onClick={() => openStudent(student)}>{student.name}</button><span>{student.entered_at.length ? `Entrée ${student.entered_at.map(value => new Date(value).toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' })).join(', ')}` : 'Aucun passage d’entrée'}</span>{student.exited_at.length > 0 && <span>Sortie {student.exited_at.map(value => new Date(value).toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' })).join(', ')}</span>}{student.late_days_last_7 >= 3 && <button className="late-alert-button" onClick={() => openStudent(student)} aria-label={`${student.name} a ${student.late_days_last_7} retards en 7 jours`}>⚠ {student.late_days_last_7} retards cette semaine · détails</button>}</div>) : <p className="muted">Aucun élève dans cette liste.</p>}</section>)}
      </div>
      {selectedStudent && <section className="student-history-panel"><div className="section-heading"><div><p className="eyebrow">JOURNÉE DE L’ÉLÈVE</p><h3>{selectedStudent.name} · {className}</h3></div><button className="secondary" onClick={() => setSelectedStudent(null)}>Fermer</button></div>{studentHistory ? <><p>Statut du jour : <strong>{statusLabel[studentHistory.status]}</strong> · {studentHistory.late_days_last_7} retard(s) sur les 7 derniers jours.</p><StudentTimeline events={studentHistory.events}/></> : <p>Chargement de son historique…</p>}</section>}
    </>}
  </div>;
}

function HourlyPassageChart({ bins }) {
  const max = Math.max(1, ...bins.flatMap(bin => [bin.entries, bin.exits]));
  return <div className="chart-wrap"><svg className="hourly-chart" viewBox="0 0 720 235" role="img" aria-label="Diagramme des entrées et sorties par heure">
    {[0, 1, 2, 3].map(index => <g key={index}><line x1="45" x2="700" y1={175 - index * 45} y2={175 - index * 45} stroke="#e7eee8"/><text x="8" y={179 - index * 45} fontSize="11" fill="#72847a">{Math.round(max * index / 3)}</text></g>)}
    {bins.map((bin, index) => { const x = 52 + index * 49; const entryHeight = bin.entries / max * 135; const exitHeight = bin.exits / max * 135; return <g key={bin.hour}><rect x={x} y={175 - entryHeight} width="15" height={entryHeight} rx="3" fill="#278553"><title>{bin.entries} entrée(s) à {bin.hour}h</title></rect><rect x={x + 18} y={175 - exitHeight} width="15" height={exitHeight} rx="3" fill="#e98a48"><title>{bin.exits} sortie(s) à {bin.hour}h</title></rect>{bin.hour % 2 === 0 && <text x={x + 5} y="198" fontSize="10" fill="#72847a">{bin.hour}h</text>}</g>; })}
  </svg><div className="chart-legend"><span><i className="legend-entry"/>Entrées</span><span><i className="legend-exit"/>Sorties</span></div></div>;
}

function StudentTimeline({ events }) {
  const xFor = timeValue => {
    const value = new Date(timeValue);
    const minute = value.getHours() * 60 + value.getMinutes();
    return 55 + Math.max(0, Math.min(1, (minute - 360) / 720)) * 620;
  };
  return <div className="student-timeline"><svg viewBox="0 0 720 190" role="img" aria-label="Chronologie des entrées et sorties de l’élève"><line x1="55" x2="675" y1="83" y2="83" stroke="#cbd8ce" strokeWidth="3"/><text x="10" y="57" fontSize="12" fill="#278553">Entrée</text><text x="10" y="132" fontSize="12" fill="#c86d32">Sortie</text>{[6, 9, 12, 15, 18].map(hour => <g key={hour}><line x1={55 + (hour - 6) / 12 * 620} x2={55 + (hour - 6) / 12 * 620} y1="72" y2="96" stroke="#b9c7bd"/><text x={55 + (hour - 6) / 12 * 620 - 9} y="115" fontSize="10" fill="#72847a">{hour}h</text></g>)}{events.map((event, index) => { const entry = event.direction === 'entry'; const x = xFor(event.time); const y = entry ? 58 : 108; return <g key={`${event.time}-${index}`}><line x1={x} x2={x} y1="83" y2={y} stroke={entry ? '#278553' : '#e98a48'} strokeWidth="2"/><circle cx={x} cy={y} r="6" fill={entry ? '#278553' : '#e98a48'}><title>{entry ? 'Entrée' : 'Sortie'} · {new Date(event.time).toLocaleTimeString('fr-FR')}</title></circle></g>; })}</svg>{events.length ? <ol className="timeline-events">{events.map((event, index) => <li key={`${event.time}-${index}`}><strong>{event.direction === 'entry' ? 'Entrée' : 'Sortie'}</strong><time>{new Date(event.time).toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' })}</time><span>{event.checkpoint || 'Point de contrôle'}</span></li>)}</ol> : <p>Aucun passage enregistré pour cet élève à cette date.</p>}</div>;
}

function AdminWorkspace({ call, students, setStudents, badges, setBadges, parents, setParents, agents, setAgents, setMessage, issueBadge, changeBadgeState }) {
  const [attendance, setAttendance] = useState(null);
  const [localError, setLocalError] = useState('');
  const [parentChoice, setParentChoice] = useState('');
  const [attendanceDate, setAttendanceDate] = useState(() => new Date().toLocaleDateString('en-CA'));
  const [busy, setBusy] = useState(false);

  const refreshAttendance = useCallback(async () => {
    try { setAttendance(await call(`/attendance/daily/?date=${encodeURIComponent(attendanceDate)}`)); setLocalError(''); }
    catch (error) { setLocalError(error.message); }
  }, [call, attendanceDate]);
  useEffect(() => {
    refreshAttendance();
    const timer = setInterval(refreshAttendance, 30000);
    return () => clearInterval(timer);
  }, [refreshAttendance]);

  async function provisionStudent(event) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    const payload = {
      name: data.get('name'), class_name: data.get('class_name'), school: data.get('school'),
      password: data.get('password'),
    };
    if (parentChoice === '__new__') payload.new_parent = {
      username: data.get('parent_username'), password: data.get('parent_password'),
      first_name: data.get('parent_first_name'), last_name: data.get('parent_last_name'),
    };
    else payload.parent_id = Number(parentChoice);
    try {
      setBusy(true); const created = await call('/students/', { method: 'POST', body: JSON.stringify(payload) });
      const [nextStudents, nextParents, nextBadges] = await Promise.all([call('/students/'), call('/parents/'), call('/badges/')]);
      setStudents(nextStudents); setParents(nextParents); setBadges(nextBadges); form.reset(); setParentChoice('');
      setMessage(`Élève créé, lié à un parent. Identifiant : ${created.login_identifier}.`); refreshAttendance();
    } catch (error) { setLocalError(error.message); }
    finally { setBusy(false); }
  }

  async function provisionAgent(event) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    try {
      setBusy(true); await call('/agents/', { method: 'POST', body: JSON.stringify({ username: data.get('username'), password: data.get('password'), first_name: data.get('first_name'), last_name: data.get('last_name') }) });
      setAgents(await call('/agents/')); form.reset(); setMessage('Compte agent créé.');
    } catch (error) { setLocalError(error.message); }
    finally { setBusy(false); }
  }

  async function createStudentAccount(event, student) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    const payload = { login_password: data.get('password') };
    if (!student.parents?.length) payload.parent_ids = data.getAll('parent_ids').map(Number);
    try {
      await call(`/students/${student.id}/`, { method: 'PATCH', body: JSON.stringify(payload) });
      const nextStudents = await call('/students/'); setStudents(nextStudents); const created = nextStudents.find(item => item.id === student.id); setMessage(`Compte créé pour ${student.name}. Identifiant : ${created?.login_identifier || 'attribué'}.`); form.reset();
    } catch (error) { setLocalError(error.message); }
  }

  return <>
    <section className="card panel attendance-panel">
      <div className="section-heading"><div><p className="eyebrow">PRÉSENCES</p><h2>Situation de l’établissement</h2></div><label>Date<input type="date" value={attendanceDate} onChange={event => setAttendanceDate(event.target.value)}/></label></div>
      {localError && <p className="message" role="alert">{localError}</p>}
      {attendance ? <>
        <p className="attendance-recap" role="status">{attendance.recap}</p>
        <div className="attendance-totals">{[['Entrés aujourd’hui', attendance.totals.entered_today], ['Entrés avant midi', attendance.totals.entered_morning], ['Présents actuellement', attendance.totals.currently_present]].map(([label, value]) => <article className="attendance-total" key={label}><small>{label}</small><strong>{value}</strong></article>)}</div>
        <h3>Répartition par classe</h3><div className="attendance-table-wrap"><table className="attendance-table"><thead><tr><th>Classe</th><th>Effectif</th><th>Entrés</th><th>Avant midi</th><th>Encore présents</th></tr></thead><tbody>{attendance.classes.map(group => <tr key={group.class_name}><td>{group.class_name}</td><td>{group.total_students}</td><td>{group.entered_today}</td><td>{group.entered_morning}</td><td>{group.currently_present}</td></tr>)}</tbody></table></div>
        <h3>Élèves actuellement dans l’établissement</h3>{attendance.present_students.length ? <div className="attendance-table-wrap"><table className="attendance-table"><thead><tr><th>Élève</th><th>Classe</th><th>Entrée</th><th>Dernier point de contrôle</th></tr></thead><tbody>{attendance.present_students.map(item => <tr key={item.id}><td>{item.name}</td><td>{item.class_name}</td><td>{new Date(item.entered_at).toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' })}</td><td>{item.checkpoint || '—'}</td></tr>)}</tbody></table></div> : <p>Aucun élève actuellement signalé présent.</p>}
        <small>Récapitulatif actualisé automatiquement · dernière mise à jour {new Date(attendance.generated_at).toLocaleTimeString('fr-FR')}</small>
      </> : <p>Chargement des présences…</p>}
    </section>

    <section className="grid admin-provision-grid">
      <article className="card panel"><p className="eyebrow">INSCRIPTIONS</p><h2>Ajouter un élève</h2><form className="admin-form" onSubmit={provisionStudent}>
        <label>Nom complet<input name="name" required maxLength="120"/></label><label>Classe<input name="class_name" required maxLength="80"/></label><label>Établissement<input name="school" required maxLength="120"/></label><p className="notice">L’identifiant de l’élève sera généré automatiquement au format AAAA0001.</p><label>Mot de passe élève<input name="password" type="password" minLength="8" required/></label>
        <label>Parent obligatoire<select value={parentChoice} onChange={event => setParentChoice(event.target.value)} required><option value="">Choisir ou créer un parent</option>{parents.map(parent => <option key={parent.id} value={parent.id}>{parent.name} · {parent.username}</option>)}<option value="__new__">Créer un nouveau parent</option></select></label>
        {parentChoice === '__new__' && <><label>Prénom du parent<input name="parent_first_name" required/></label><label>Nom du parent<input name="parent_last_name" required/></label><label>Email du parent<input name="parent_username" type="email" required/></label><label>Mot de passe du parent<input name="parent_password" type="password" minLength="8" required/></label></>}
        <button disabled={busy || !parentChoice}>Créer l’élève</button>
      </form></article>
      <article className="card panel"><p className="eyebrow">ÉQUIPE</p><h2>Ajouter un agent</h2><form className="admin-form" onSubmit={provisionAgent}><label>Prénom<input name="first_name" required/></label><label>Nom<input name="last_name" required/></label><label>Identifiant email<input name="username" type="email" required/></label><label>Mot de passe<input name="password" type="password" minLength="8" required/></label><button disabled={busy}>Créer l’agent</button></form><h3>Agents actifs ({agents.length})</h3>{agents.map(agent => <p className="notice" key={agent.id}>{agent.name} · {agent.username}</p>)}</article>
    </section>

    <section className="card panel"><p className="eyebrow">ÉLÈVES ET BADGES</p><h2>Comptes élèves</h2><p>Le QR personnel est accessible uniquement depuis le compte de l’élève. L’administration peut gérer l’état du badge sans afficher son QR.</p>{students.map(student => <div className="student-row student-management" key={student.id}><div><strong>{student.name}</strong><small> · {student.class_name} · Parent(s) : {student.parents?.map(parent => parent.name).join(', ') || 'À rattacher'}</small>{student.login_identifier && <small className="account-ready">Identifiant élève : {student.login_identifier}</small>}{!student.has_account && <form className="student-account-form" onSubmit={event => createStudentAccount(event, student)}><input name="password" type="password" minLength="8" required placeholder="Mot de passe élève (8 caractères min.)"/>{!student.parents?.length && <select name="parent_ids" required multiple aria-label="Parents obligatoires">{parents.map(parent => <option key={parent.id} value={parent.id}>{parent.name} · {parent.username}</option>)}</select>}<button className="secondary">Créer le compte</button></form>}{student.has_account && <small className="account-ready">Compte élève prêt</small>}</div><button className="secondary" onClick={() => issueBadge(student)}>Émettre / remplacer le badge</button></div>)}<h3>État des badges</h3>{badges.map(badge => <div className="student-row" key={badge.badge_id}><span>{students.find(student => student.id === badge.student_id)?.name || 'Élève'} · {badge.state}</span><div>{['suspended', 'lost', 'revoked'].map(state => <button key={state} className="secondary" disabled={badge.state === 'revoked' || badge.state === state} onClick={() => changeBadgeState(badge, state)}>{state === 'suspended' ? 'Suspendre' : state === 'lost' ? 'Signaler perdu' : 'Révoquer'}</button>)}</div></div>)}</section>
  </>;
}

function ParentView({ call }) {
  const [items, setItems] = useState([]);
  const [scans, setScans] = useState([]);
  const [error, setError] = useState('');
  useEffect(() => {
    Promise.all([call('/anomalies/'), call('/scans/')])
      .then(([anomalies, recentScans]) => {
        setItems(anomalies);
        setScans(recentScans);
      })
      .catch(requestError => setError(requestError.message));
  }, [call]);
  return <>
    {error && <p className="message" role="alert">{error}</p>}
    <h3>Passages récents</h3>
    {scans.length
      ? scans.slice(0, 10).map(scan => <p key={scan.id} className="notice">{scan.checkpoint} · {new Date(scan.estimated_time || scan.received_at).toLocaleString('fr-FR')}{scan.clock_suspect ? ' · heure à vérifier' : ''}</p>)
      : <p>Aucun passage à afficher.</p>}
    <h3>Anomalies</h3>
    {items.length
      ? items.map(item => <p key={item.id} className="notice">{item.student.name} · {item.details || item.kind}</p>)
      : <p>Aucune anomalie à afficher.</p>}
  </>;
}
