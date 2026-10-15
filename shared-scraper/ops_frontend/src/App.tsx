import { useCallback, useEffect, useState } from 'react';
import { Activity, ArrowRight, CheckCircle2, Clock3, CalendarClock, Database, FileCheck2, LayoutDashboard, LockKeyhole, RefreshCw, Server, Settings2, Shield, Users, Wifi, ChevronLeft, ChevronRight, CreditCard, ScrollText, Mail } from 'lucide-react';
import FleetMachines from './FleetMachines';
import PortalAccounts from './PortalAccounts';
import WorkQueue from './WorkQueue';
import ManageUsers from '../../tm_manager/frontend/src/pages/ManageUsers';
import ManagePlans from '../../tm_manager/frontend/src/pages/ManagePlans';
import ManageSubscriptions from '../../tm_manager/frontend/src/pages/ManageSubscriptions';
import { useAuth } from '../../tm_manager/frontend/src/context/AuthContext';

const workerMode = import.meta.env.MODE === 'worker';
const base = workerMode ? '/api/worker' : '/api/control/v1/admin';
const credentialKey = workerMode ? 'worker-local-credential' : 'fleet-control-credential';
const menus = workerMode
  ? [['Overview', 'overview', LayoutDashboard], ['Assignments & results', 'assignments', FileCheck2], ['Activity log', 'events', ScrollText], ['Worker controls', 'controls', Settings2]] as const
  : [['Overview', 'overview', LayoutDashboard], ['Fleet machines', 'workers', Server], ['Portal accounts', 'accounts', Mail], ['Work queue', 'jobs', Activity], ['Status schedule', 'status-schedule', CalendarClock], ['Refresh policies', 'policies', Settings2], ['Results & documents', 'results', FileCheck2], ['Document coverage', 'document_ledger', Database], ['Opposition records', 'proceedings', ScrollText], ['Compliance facts', 'compliance_events', CalendarClock], ['Users', 'users', Users], ['Plans', 'plans', CreditCard], ['Subscriptions', 'subscriptions', CreditCard], ['Audit log', 'audit', ScrollText]] as const;

function Badge({ value }: { value: unknown }) {
  const text = String(value ?? '—');
  const positive = ['accepted', 'completed', 'acknowledged', 'active', 'idle'].includes(text);
  const negative = ['error', 'failed', 'retired', 'obsolete'].includes(text);
  return <span className={`ops-badge ${positive ? 'good' : negative ? 'bad' : ''}`}><i />{text.replaceAll('_', ' ')}</span>;
}
function shown(value: any): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'object') return JSON.stringify(value);
  if (typeof value === 'string' && /^\d{4}-\d\d-\d\dT/.test(value)) return new Date(value).toLocaleString();
  return String(value);
}

export default function App() {
  const [page, setPage] = useState('overview');
  const [token, setToken] = useState(() => sessionStorage.getItem(credentialKey) || '');
  const [credential, setCredential] = useState('');
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<any>(null);
  const [revealed, setRevealed] = useState('');
  const { user, login, logout } = useAuth();
  const userPage = ['users', 'plans', 'subscriptions'].includes(page);
  const enabled = workerMode ? Boolean(token) : Boolean(user?.is_admin);

  const api = useCallback(async (path: string, method = 'GET', body?: any) => {
    const response = await fetch(base + path, { method, headers: { ...(workerMode ? { Authorization: `Bearer ${token}` } : { 'X-Fleet-Request': '1' }), ...(body ? { 'Content-Type': 'application/json' } : {}) }, body: body ? JSON.stringify(body) : undefined });
    const json = await response.json();
    if (!response.ok) throw new Error(json.error || `Request failed (${response.status})`);
    return json;
  }, [token]);

  const load = useCallback(async () => {
    if (!enabled || userPage) return;
    try {
      const path = workerMode ? page === 'overview' || page === 'controls' ? '/status' : `/${page}` : `/${page}?limit=50&offset=${offset}`;
      setData(await api(path));
      setError('');
    } catch (err) { setError((err as Error).message); }
  }, [api, enabled, page, offset, userPage]);
  useEffect(() => { setData(null); setSelected(null); load(); const timer = setInterval(load, 10000); return () => clearInterval(timer); }, [load]);

  async function action(path: string, method = 'POST', body?: any) {
    setBusy(true); setNotice(''); setError('');
    try {
      const response = await api(path, method, body || {});
      if (response.token) setRevealed(response.token);
      setNotice(typeof response.updated_trademarks === 'number' ? `Saved. ${response.updated_trademarks} trademark(s) moved to the new schedule.` : 'Saved successfully.');
      await load();
    } catch (err) { setError((err as Error).message); }
    finally { setBusy(false); }
  }
  function navigate(id: string) { setPage(id); setOffset(0); setNotice(''); setRevealed(''); }
  const title = menus.find(m => m[1] === page)?.[0] || 'Overview';
  const rows: any[] = data?.items || [];
  const columns = workerMode
    ? page === 'events' ? ['at', 'message'] : ['app_no', 'state', 'disposition', 'updated_at', 'error']
    : ({ workers: ['name', 'status', 'phase', 'capacity', 'last_contact_at', 'offline_until', 'metadata'], accounts: ['email', 'worker_id', 'enabled', 'created_at'], jobs: ['app_no', 'state', 'priority', 'attempts', 'due_at', 'expires_at', 'last_error'], policies: ['name', 'id', 'interval_days', 'match_status'], results: ['app_no', 'disposition', 'generation', 'observed_at', 'received_at'], document_ledger: ['app_no', 'source_key', 'download_state', 'parse_state', 'observed_at'], proceedings: ['app_no', 'opposition_no', 'portal_type', 'observed_at'], compliance_events: ['app_no', 'opposition_no', 'category', 'due_date', 'due_basis', 'needs_review'], audit: ['action', 'actor', 'created_at', 'details'] } as Record<string, string[]>)[page] || [];

  return <div className="ops-app">
    <aside className="ops-sidebar">
      <div className="ops-brand"><span className="ops-brand-icon"><Activity size={22} /></span><div>TM Tracker<small>{workerMode ? 'LOCAL WORKER' : 'CONTROL PLANE'}</small></div></div>
      <div className="ops-nav-label">{workerMode ? 'THIS MACHINE' : 'OPERATIONS'}</div>
      <nav>{menus.map(([label, id, Icon]) => <button key={id} className={page === id ? 'active' : ''} onClick={() => navigate(id)}><Icon size={17} />{label}{page === id && <ArrowRight size={14} className="ops-nav-arrow" />}</button>)}</nav>
      <div className="ops-sidebar-foot"><Shield size={17} /><div>{workerMode ? 'Results saved locally' : 'Central coordination'}<small>{workerMode ? 'Retained until server acknowledgment' : 'Users and workers stay separate'}</small></div></div>
    </aside>
    <main className="ops-main">
      <header className="ops-header"><div className="ops-breadcrumb">{workerMode ? 'Worker workspace' : 'Administration'}<span>/</span>{title}</div><div className="ops-header-right"><span className="ops-live"><i />{workerMode ? data?.mode || 'Local agent' : 'Contabo coordinator'}</span>{!workerMode && user && <button className="ops-text-button" onClick={logout}>Sign out</button>}{workerMode && token && <button className="ops-text-button" onClick={() => { sessionStorage.removeItem(credentialKey); setToken(''); }}>Lock session</button>}</div></header>
      <div className="ops-content">
        <div className="ops-title-row"><div><div className="ops-eyebrow">{workerMode ? 'SCRAPE. SAVE. DELIVER.' : 'TRADEMARK OPERATIONS'}</div><h1>{title}</h1><p>{workerMode ? 'Track the entire journey from assignment to server acknowledgment.' : 'Coordinate your fleet and keep trademark data current.'}</p></div>{enabled && !userPage && <button className="ops-button secondary" disabled={busy} onClick={load}><RefreshCw size={15} />Refresh</button>}</div>
        {error && <div className="ops-alert bad" role="alert">{error}</div>}{notice && <div className="ops-alert good" role="status">{notice}</div>}
        {revealed && <div className="ops-alert"><strong>Copy this worker credential now. It is shown only once.</strong><code>{revealed}</code><button className="ops-button secondary" onClick={() => navigator.clipboard.writeText(revealed)}>Copy credential</button></div>}
        {!workerMode && !user ? <UserSignIn login={login} /> : !workerMode && !user?.is_admin ? <div className="ops-alert bad">Administrator access is required.</div> : workerMode && !token ? <section className="ops-card ops-unlock"><LockKeyhole size={30} /><h2>{workerMode ? 'Connect to your local worker' : 'Unlock fleet operations'}</h2><p>Enter the {workerMode ? 'local worker' : 'control plane administrator'} credential. It stays in this browser tab’s session.</p><form onSubmit={event => { event.preventDefault(); sessionStorage.setItem(credentialKey, credential); setToken(credential); setCredential(''); }}><input type="password" autoComplete="off" value={credential} onChange={e => setCredential(e.target.value)} placeholder="Access credential" required /><button className="ops-button">Continue<ArrowRight size={16} /></button></form></section>
          : userPage ? !user ? <UserSignIn login={login} /> : !user.is_admin ? <div className="ops-alert bad">Administrator access is required.</div> : <section className="ops-user-section"><div className="ops-user-account">Signed in as {user.email}<button className="ops-text-button" onClick={logout}>Sign out</button></div>{page === 'users' ? <ManageUsers /> : page === 'plans' ? <ManagePlans /> : <ManageSubscriptions />}</section>
          : page === 'overview' ? <>
            <div className="ops-stats">{(workerMode ? [['Current phase', data?.phase, Activity], ['Saved results', data?.counts?.upload_pending || 0, Database], ['Server acknowledged', data?.counts?.acknowledged || 0, CheckCircle2], ['Free disk', data ? `${(data.free_bytes / 1073741824).toFixed(1)} GB` : '—', Server]] : [['Fleet machines', data?.worker_count || 0, Server], ['Queued work', data?.jobs?.queued || 0, Activity], ['Reserved jobs', data?.jobs?.reserved || 0, Wifi], ['Due trademarks', data?.overdue || 0, Clock3]]).map(([label, value, Icon]: any) => <section className="ops-stat" key={label}><span>{label}<Icon size={19} /></span><strong>{value ?? '—'}</strong><small>{workerMode ? 'Durable local tracking' : 'Live coordinator state'}</small></section>)}</div>
            <section className="ops-card"><div className="ops-card-heading"><h2>{workerMode ? 'The worker cycle' : 'Work moves through your fleet'}</h2><span className="ops-muted">Recovery at every step</span></div><div className="ops-flow">{(workerMode ? (data?.network?.requested_mode === 'split' ? ['Receive assignments', 'Verify VPN routes', 'Scrape & save', 'Deliver when connected', 'Recover & confirm'] : ['Receive assignments', 'Connect VPN', 'Scrape & save', 'Disconnect VPN', 'Upload & confirm']) : ['TM becomes due', 'Reserve on worker', 'Scrape & save', 'Receive results', 'Update portfolios']).map((label, index) => <div key={label}><span>{String(index + 1).padStart(2, '0')}</span><strong>{label}</strong>{index < 4 && <ArrowRight size={18} />}</div>)}</div></section>
            {workerMode ? <section className="ops-card"><h2>Network connectivity</h2><NetworkStatus network={data?.network} /></section> : <section className="ops-card"><h2>Fleet connectivity</h2><div className="ops-state-list">{Object.entries(data?.worker_connectivity || {}).map(([state, count]) => <div key={state}><Badge value={state} /><strong>{String(count)}</strong></div>)}</div></section>}
            <div className="ops-two-column"><section className="ops-card"><h2>{workerMode ? 'Delivery state' : 'Job health'}</h2><div className="ops-state-list">{Object.entries(workerMode ? data?.counts || {} : data?.jobs || {}).map(([state, count]) => <div key={state}><Badge value={state} /><strong>{String(count)}</strong></div>)}{!Object.keys(workerMode ? data?.counts || {} : data?.jobs || {}).length && <p className="ops-muted">No jobs yet. {workerMode ? 'Start a cycle to receive work.' : 'Add a trademark request to begin.'}</p>}</div></section><section className="ops-card"><h2>{workerMode ? 'Worker controls' : 'Queue a priority fetch'}</h2>{workerMode ? <><p className="ops-muted">Pause stops new scraping. Saved results can still upload.</p><div className="ops-actions"><button className="ops-button" disabled={busy} onClick={() => action('/cycle')}>Run cycle</button><button className="ops-button secondary" onClick={() => action(data?.paused ? '/resume' : '/pause')}>{data?.paused ? 'Resume' : 'Pause'}</button></div></> : <Form fields={[['app_no', 'Application number', 'text']]} label="Queue fetch" busy={busy} submit={body => action('/jobs', 'POST', { ...body, priority: 150 })} />}</section></div>
          </> : page === 'controls' && workerMode ? <section className="ops-card"><h2>Worker controls</h2><p className="ops-muted">Every cycle uploads saved results before receiving more work. {data?.network?.requested_mode === 'split' ? 'During scraping, delivery requires verified coordinator connectivity. Outages retain saved results for retry.' : 'Offline mode disconnects the VPN before coordinator delivery.'}</p><NetworkStatus network={data?.network} /><div className="ops-actions"><button className="ops-button" disabled={busy} onClick={() => action('/cycle')}>Run cycle / retry delivery</button><button className="ops-button secondary" onClick={() => action('/pause')}>Pause & drain</button><button className="ops-button secondary" onClick={() => action('/resume')}>Resume</button></div><p className="ops-muted">Coordinator: {data?.coordinator_url}</p><Badge value={data?.phase} /></section>
          : <>
            {!workerMode && page === 'workers' && <FleetMachines rows={rows} api={api} reload={load} goTo={navigate} />}
            {!workerMode && page === 'accounts' && <PortalAccounts api={api} goTo={navigate} />}
            {!workerMode && page === 'jobs' && <WorkQueue api={api} goTo={navigate} />}
            {!workerMode && page === 'policies' && <section className="ops-card"><h2>Add or update a policy</h2><Form fields={[['id', 'Policy ID (letters/underscores)', 'text'], ['name', 'Display name', 'text'], ['interval_days', 'Days (blank pauses)', 'number', 'optional'], ['match_status', 'Exact portal status (optional rule)', 'text', 'optional']]} label="Save policy" busy={busy} submit={body => action('/policies', 'POST', { ...body, interval_days: body.interval_days ? Number(body.interval_days) : null })} /><p className="ops-muted">Use the Paused policy to stop automatic checks. Long-term reminders are managed separately.</p></section>}
            {!workerMode && page === 'status-schedule' && <StatusSchedule data={data} busy={busy} save={(status, days) => action('/status-schedule', 'POST', { status, interval_days: days })} fillMissing={() => action('/status-schedule/defaults', 'POST', {})} />}
            {page !== 'status-schedule' && !(['workers', 'accounts', 'jobs'].includes(page) && !workerMode) && <section className="ops-card ops-table-card"><div className="ops-card-heading"><h2>{title}</h2><span className="ops-muted">{data?.total ?? rows.length} records</span></div><div className="ops-table-wrap"><table><thead><tr>{columns.map(column => <th key={column}>{column === 'metadata' ? 'Network' : column.replaceAll('_', ' ')}</th>)}<th>Details</th></tr></thead><tbody>{rows.map((raw, index) => { const row = workerMode && page === 'assignments' ? { ...raw, app_no: raw.assignment?.app_no } : raw; return <tr key={row.id || row.key || index}>{columns.map(column => <td key={column}>{column === 'metadata' ? <NetworkCell network={row.metadata?.network} /> : ['state', 'status', 'phase', 'disposition'].includes(column) ? <Badge value={row[column]} /> : <span title={shown(row[column])}>{column === 'updated_at' || column === 'at' ? new Date(row[column] * 1000).toLocaleString() : shown(row[column])}</span>}</td>)}<td><button className="ops-text-button" onClick={async () => { try { setSelected(page === 'results' ? await api(`/results/${row.id}`) : raw); } catch (err) { setError((err as Error).message); } }}>Inspect</button></td></tr>; })}</tbody></table>{!rows.length && <div className="ops-empty"><FileCheck2 size={28} /><h3>No records to show</h3><p>{error ? 'Check the connection message above.' : 'New work and activity will appear here.'}</p></div>}</div>{!workerMode && <div className="ops-pagination"><span>{offset + (rows.length ? 1 : 0)}–{offset + rows.length} of {data?.total || 0}</span><div><button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}><ChevronLeft size={17} /></button><button disabled={offset + rows.length >= (data?.total || 0)} onClick={() => setOffset(offset + 50)}><ChevronRight size={17} /></button></div></div>}</section>}
          </>}
      </div>
    </main>
    {selected && <div className="ops-modal-backdrop" onClick={() => setSelected(null)}><section className="ops-modal" role="dialog" aria-modal="true" aria-label="Record details" onClick={e => e.stopPropagation()}><div className="ops-card-heading"><h2>Record details</h2><button className="ops-text-button" onClick={() => setSelected(null)}>Close</button></div>{page === 'workers' && <NetworkStatus network={selected.metadata?.network} />}{page === 'workers' && <div className="ops-actions">{['active', 'paused', 'draining', 'retired'].map(status => <button className="ops-button secondary" key={status} onClick={() => { if (status !== 'retired' || confirm('Retire this worker and revoke its access?')) action(`/workers/${selected.id}`, 'PATCH', { status, capacity: selected.capacity }); }}>{status}</button>)}<button className="ops-button secondary" onClick={() => action(`/workers/${selected.id}/rotate-token`)}>Rotate credential</button></div>}{page === 'accounts' && <Form fields={[['worker_id', 'Assigned worker ID (blank unassigns)', 'text', 'optional']]} label="Assign account" busy={busy} submit={body => action(`/accounts/${selected.id}`, 'PATCH', { worker_id: body.worker_id, enabled: true })} />}{page === 'accounts' && <button className="ops-button secondary" onClick={() => action(`/accounts/${selected.id}`, 'PATCH', {worker_id: selected.worker_id, enabled: !selected.enabled})}>{selected.enabled ? 'Disable account' : 'Enable account'}</button>}{page === 'jobs' && <div className="ops-actions"><button className="ops-button" onClick={() => action(`/jobs/${selected.id}`, 'PATCH', {action: 'retry', priority: 150})}>Retry now</button><button className="ops-button secondary" onClick={() => { if (confirm('Cancel this assignment? Late results will be archived.')) action(`/jobs/${selected.id}`, 'PATCH', {action: 'cancel'}); }}>Cancel job</button></div>}{page === 'jobs' && <Form fields={[['priority', 'Priority (1-1000)', 'number']]} label="Set priority" busy={busy} submit={body => action(`/jobs/${selected.id}`, 'PATCH', {action: 'priority', priority: Number(body.priority)})} />}<div className="ops-actions">{workerMode && selected.result && <button className="ops-button secondary" disabled={busy} onClick={() => action('/reextract', 'POST', {result_id: selected.result.result_id})}>Retry extraction from saved files</button>}{!workerMode && page === 'jobs' && <button className="ops-button secondary" disabled={busy} onClick={() => action(`/jobs/${selected.id}`, 'PATCH', {action: 'force_full'})}>Force full download</button>}</div><pre>{JSON.stringify(selected, null, 2)}</pre>{page === 'results' && selected.payload?.artifacts?.map((a: any) => <button className="ops-button secondary" key={a.sha256} onClick={async () => { try { const response = await fetch(`${base}/results/${selected.id}/artifacts/${a.sha256}`, { headers: workerMode ? { Authorization: `Bearer ${token}` } : { 'X-Fleet-Request': '1' } }); if (!response.ok) throw new Error('Download failed'); const url = URL.createObjectURL(await response.blob()); const anchor = document.createElement('a'); anchor.href = url; anchor.download = a.filename; anchor.click(); URL.revokeObjectURL(url); } catch (err) { setError((err as Error).message); } }}>{a.filename}</button>)}</section></div>}
  </div>;
}

function StatusSchedule({ data, busy, save, fillMissing }: { data: any; busy: boolean; save: (status: string, days: number | null) => void; fillMissing: () => void }) {
  const [draft, setDraft] = useState<Record<string, string>>({});
  const items: any[] = data?.items || [];
  const current = (row: any) => draft[row.status] ?? (row.interval_days === null || row.interval_days === undefined ? '' : String(row.interval_days));
  const saved = (row: any) => (row.interval_days === null || row.interval_days === undefined ? '' : String(row.interval_days));
  return <section className="ops-card ops-table-card">
    <div className="ops-card-heading"><h2>Fetch every N days, by status</h2><span className="ops-muted">{data?.known_status_trademarks ?? 0} of {data?.trademarks_total ?? 0} trademarks have a known status</span></div>
    <p className="ops-muted ops-status-help">While a trademark is in a status, it is fetched again every N days. Saving moves every trademark that is in that status to the new interval straight away. Leave the days empty to stop fetching in that status. A trademark whose status has no rule stays on its current schedule.</p>
    <div className="ops-status-actions"><button className="ops-button secondary" disabled={busy || !items.length} onClick={fillMissing}>Fill missing with recommended days</button></div>
    <div className="ops-table-wrap"><table><thead><tr><th>Portal status</th><th>Fetch every (days)</th><th>Trademarks</th><th>Recommended</th><th></th></tr></thead><tbody>
      {items.map(row => <tr key={row.status}>
        <td><span title={row.known ? '' : 'Returned by the portal but not in the standard list'}>{row.status}</span>{!row.known && <span className="ops-muted"> (new)</span>}{!row.has_rule && <span className="ops-muted"> — no rule</span>}</td>
        <td><input className="ops-days-input" type="number" min={1} max={3650} placeholder="never" value={current(row)} onChange={e => setDraft({ ...draft, [row.status]: e.target.value })} /></td>
        <td>{row.trademarks}</td>
        <td className="ops-muted">{row.recommended_days === null || row.recommended_days === undefined ? (row.known ? 'never' : '—') : row.recommended_days}</td>
        <td><button className="ops-button" disabled={busy || current(row) === saved(row) && row.has_rule} onClick={() => { save(row.status, current(row) === '' ? null : Number(current(row))); setDraft(({ [row.status]: _gone, ...rest }) => rest); }}>Save</button></td>
      </tr>)}
    </tbody></table>{!items.length && <div className="ops-empty"><FileCheck2 size={28} /><h3>No statuses to show</h3><p>Check the connection message above.</p></div>}</div>
  </section>;
}

function NetworkCell({ network }: { network: any }) {
  if (!network) return <span>Not checked</span>;
  return <div title={network.reason || 'Open details for the last connectivity checks'}><Badge value={network.effective_mode} /><small className="ops-muted">{network.simulated ? 'Fixture simulation' : `Requested: ${network.requested_mode}`}</small></div>;
}

function NetworkStatus({ network }: { network: any }) {
  if (!network) return <p className="ops-muted">Connectivity checks appear when the worker runs a cycle.</p>;
  return <><div className="ops-state-list">{[['Requested mode', network.requested_mode], ['Current delivery mode', network.effective_mode], ['Last portal check', network.portal], ['Last coordinator check', network.coordinator]].map(([label, value]) => <div key={label}><span>{label}</span><Badge value={value} /></div>)}</div>{network.simulated && <p className="ops-muted">Fixture simulation. Live VPN connectivity has not been verified.</p>}{network.checked_at && <p className="ops-muted">Last checked: {new Date(network.checked_at).toLocaleString()}</p>}{network.reason && <p className="ops-alert bad" role="status">{network.reason}</p>}</>;
}

function Form({ fields, label, busy, submit }: { fields: string[][]; label: string; busy: boolean; submit: (body: Record<string, string>) => void }) {
  return <form className="ops-form" onSubmit={event => { event.preventDefault(); const form = event.currentTarget; submit(Object.fromEntries(new FormData(form).entries()) as Record<string, string>); }}>{fields.map(([name, label, type, optional]) => <label key={name}>{label}<input name={name} type={type} required={optional !== 'optional'} min={type === 'number' ? 1 : undefined} /></label>)}<button className="ops-button" disabled={busy}>{busy ? 'Saving…' : label}<ArrowRight size={15} /></button></form>;
}

function UserSignIn({ login }: { login: (email: string, password: string, remember: boolean) => Promise<{ ok: boolean; error?: string }> }) {
  const [error, setError] = useState('');
  return <section className="ops-card ops-unlock"><Users size={30} /><h2>Sign in to administration</h2><p>User and subscription management uses your existing administrator account.</p>{error && <div className="ops-alert bad">{error}</div>}<form onSubmit={async event => { event.preventDefault(); const values = new FormData(event.currentTarget); const response = await login(String(values.get('email')), String(values.get('password')), false); if (!response.ok) setError(response.error || 'Sign-in failed.'); }}><input name="email" type="email" placeholder="Administrator email" autoComplete="username" required /><input name="password" type="password" placeholder="Password" autoComplete="current-password" required /><button className="ops-button">Sign in</button></form></section>;
}
