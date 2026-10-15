import { useCallback, useEffect, useMemo, useState } from 'react';
import { Plus } from 'lucide-react';
import { ActionRow, bytes, clock, CopyBox, Drawer, Facts, PageIntro, Section, timeAgo, Tone } from './ui';

type Props = {
  rows: any[];
  api: (path: string, method?: string, body?: any) => Promise<any>;
  reload: () => Promise<void> | void;
  goTo: (page: string) => void;
};

const STATUS: Record<string, { label: string; tone: 'good' | 'bad' | ''; hint: string }> = {
  pending: { label: 'Waiting for approval', tone: '', hint: 'Waiting for you to name and approve it' },
  active: { label: 'Active', tone: 'good', hint: 'Receiving work' },
  paused: { label: 'Paused', tone: '', hint: 'Not receiving new work' },
  draining: { label: 'Finishing up', tone: '', hint: 'Finishing its batch, then it stops' },
  retired: { label: 'Removed', tone: 'bad', hint: 'Disconnected for good' },
};

const PHASE: Record<string, string> = {
  idle: 'Waiting for work', reserved: 'Has a batch, about to start', scraping: 'Scraping the portal', syncing: 'Checking in',
  upload_pending: 'Sending results', extracting: 'Reading saved documents', error: 'Needs attention', paused: 'Paused',
};

const OUTCOME: Record<string, string> = {
  accepted: 'Saved', partial: 'Saved, some documents missing', obsolete: 'Skipped, newer data already existed',
};

function connection(w: any): { tone: 'good' | 'bad' | ''; text: string } {
  if (w.status === 'retired') return { tone: '', text: 'Removed' };
  const now = Date.now();
  const last = w.last_contact_at ? Date.parse(w.last_contact_at) : 0;
  if (!last) return { tone: '', text: 'Never connected' };
  if (now - last < 10 * 60e3) return { tone: 'good', text: 'Online' };
  if (w.offline_until && Date.parse(w.offline_until) > now) return { tone: 'good', text: `Working offline, due back by ${clock(w.offline_until)}` };
  return { tone: 'bad', text: 'Not reachable' };
}

function randomKey(length = 32): string {
  const alphabet = 'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  const values = crypto.getRandomValues(new Uint32Array(length));
  return Array.from(values, v => alphabet[v % alphabet.length]).join('');
}

function envFile(token: string, batch: number): string {
  return [`WORKER_COORDINATOR_URL=${window.location.origin}`, `WORKER_TOKEN=${token}`, `WORKER_LOCAL_TOKEN=${randomKey()}`,
    'WORKER_MODE=live', `WORKER_BATCH_SIZE=${batch}`, 'WORKER_VPN_CONFIG=CHANGE_ME_name_of_this_PCs_vpn_file'].join('\n');
}

function enrollEnv(key: string): string {
  return [`WORKER_COORDINATOR_URL=${window.location.origin}`, `WORKER_ENROLL_KEY=${key}`, `WORKER_LOCAL_TOKEN=${randomKey()}`,
    'WORKER_MODE=live', 'WORKER_VPN_CONFIG=CHANGE_ME_name_of_this_PCs_vpn_file'].join('\n');
}

export default function FleetMachines({ rows, api, reload, goTo }: Props) {
  const [showRemoved, setShowRemoved] = useState(false);
  const [adding, setAdding] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  const [approved, setApproved] = useState('');
  const waiting = rows.filter(w => w.status === 'pending');
  const visible = useMemo(() => rows.filter(w => w.status !== 'pending' && (showRemoved || w.status !== 'retired')), [rows, showRemoved]);
  const removed = rows.filter(w => w.status === 'retired').length;
  const counts: Record<string, number> = {};
  rows.forEach(w => { counts[w.status] = (counts[w.status] || 0) + 1; });

  return <>
    <PageIntro action={<button className="ops-button" onClick={() => setAdding(true)}><Plus size={15} />Add a machine</button>}>
      A machine is a PC that scrapes the IP India portal for you. It takes trademarks from the queue, collects their status and
      documents, and sends everything back here. To add one, create an enrollment key and start the worker on the PC. It then
      appears below by itself and you name and approve it. After that, give it portal accounts.
    </PageIntro>
    {approved && <div className="ops-alert good" role="status"><strong>{approved}</strong> is approved and will start taking work.
      <div><button className="ops-text-button" onClick={() => goTo('accounts')}>Give it portal accounts</button></div></div>}
    {waiting.length > 0 && <WaitingMachines machines={waiting} api={api} reload={reload} onApproved={setApproved} />}
    <section className="ops-card ops-table-card">
      <div className="ops-card-heading">
        <h2>Your machines</h2>
        <span className="ops-muted">{[counts.active && `${counts.active} active`, counts.paused && `${counts.paused} paused`, counts.draining && `${counts.draining} finishing up`].filter(Boolean).join(' · ') || 'None running'}</span>
      </div>
      <div className="ops-table-wrap"><table>
        <thead><tr><th>Machine</th><th>Status</th><th>Right now</th><th>Connection</th><th>Batch size</th><th /></tr></thead>
        <tbody>{visible.map(w => {
          const s = STATUS[w.status] || { label: w.status, tone: '' as const, hint: '' };
          const c = connection(w);
          return <tr key={w.id} className="ops-clickable" onClick={() => setOpenId(w.id)}>
            <td><strong>{w.name}</strong><div className="ops-muted">Added {timeAgo(w.created_at)}</div></td>
            <td><Tone tone={s.tone}>{s.label}</Tone></td>
            <td>{w.status === 'retired' ? '—' : PHASE[w.phase] || w.phase}</td>
            <td><Tone tone={c.tone}>{c.text}</Tone>{w.status !== 'retired' && <div className="ops-muted" title={w.last_contact_at || ''}>Last seen {timeAgo(w.last_contact_at)}</div>}</td>
            <td>{w.capacity}</td>
            <td><button className="ops-button secondary" onClick={e => { e.stopPropagation(); setOpenId(w.id); }}>Manage</button></td>
          </tr>;
        })}</tbody>
      </table>
      {!visible.length && <div className="ops-empty"><h3>No machines yet</h3><p>Add your first machine to start collecting trademark data.</p></div>}
      </div>
      {removed > 0 && <div className="ops-table-foot"><button className="ops-text-button" onClick={() => setShowRemoved(!showRemoved)}>{showRemoved ? 'Hide removed machines' : `Show ${removed} removed machine${removed === 1 ? '' : 's'}`}</button></div>}
    </section>
    {adding && <AddMachine api={api} reload={reload} goTo={goTo} onClose={() => setAdding(false)} />}
    {openId && <MachineDetails id={openId} api={api} reload={reload} goTo={goTo} row={rows.find(w => w.id === openId)} onClose={() => setOpenId(null)} />}
  </>;
}

function AddMachine({ api, reload, goTo, onClose }: { api: Props['api']; reload: Props['reload']; goTo: Props['goTo']; onClose: () => void }) {
  const [byHand, setByHand] = useState(false);
  return byHand
    ? <ManualRegister api={api} reload={reload} goTo={goTo} onClose={onClose} onBack={() => setByHand(false)} />
    : <EnrollDrawer api={api} onClose={onClose} onManual={() => setByHand(true)} />;
}

function EnrollDrawer({ api, onClose, onManual }: { api: Props['api']; onClose: () => void; onManual: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [made, setMade] = useState<{ key: string } | null>(null);
  const [keys, setKeys] = useState<any[]>([]);

  const loadKeys = useCallback(async () => {
    try { setKeys((await api('/enrollment-keys')).items); } catch { /* the create button reports problems */ }
  }, [api]);
  useEffect(() => { loadKeys(); }, [loadKeys]);

  async function create() {
    setBusy(true); setError('');
    try { setMade(await api('/enrollment-keys', 'POST', { label: 'Created in administration' })); await loadKeys(); }
    catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  }
  async function revoke(id: string) {
    if (!window.confirm('Revoke this key? PCs that have not enrolled with it yet will no longer be able to use it. Machines already enrolled keep working.')) return;
    try { await api(`/enrollment-keys/${id}/revoke`, 'POST', {}); await loadKeys(); } catch (err) { setError((err as Error).message); }
  }
  const active = keys.filter(k => !k.revoked_at && (!k.expires_at || Date.parse(k.expires_at) > Date.now()));

  return <Drawer title="Add a machine" subtitle="The new PC introduces itself, then you name and approve it" onClose={onClose}>
    {error && <div className="ops-alert bad" role="alert">{error}</div>}
    {!made ? <div className="ops-stack">
      <p>Create an enrollment key, put it on the new PC and start the worker there. The PC then shows up on this page by itself,
        waiting for you to name and approve it. Until you approve it, it gets no work and no portal accounts.</p>
      <p className="ops-muted">The key works for any number of PCs and never expires on its own. You can revoke it yourself at any time.</p>
      <button className="ops-button" disabled={busy} onClick={create}>{busy ? 'Creating…' : 'Create enrollment key'}</button>
    </div> : <ol className="ops-steps">
      <li>
        <strong>Copy these settings to the new PC.</strong>
        <p className="ops-muted">Save them in a file called <code>worker.env</code> on the PC. The key is shown only now. It works for any number of PCs until you revoke it. Change the last line to the name of that PC's VPN file (without .conf).</p>
        <CopyBox text={enrollEnv(made.key)} label="Copy settings" />
      </li>
      <li>
        <strong>Start the worker on that PC.</strong>
        <p className="ops-muted">Open PowerShell as administrator in the TM Tracker folder and run this.</p>
        <CopyBox text={'.\\run-all.ps1 -Mode worker -ConfigFile .\\worker.env'} />
      </li>
      <li>
        <strong>Approve it here.</strong>
        <p className="ops-muted">Within a minute the PC appears at the top of this page as a new machine. Give it a name and press Approve. You can close this panel and wait there.</p>
        <button className="ops-button secondary" onClick={onClose}>Done</button>
      </li>
    </ol>}
    <Section title="Keys that still work" hint="Keys stay valid until you revoke them. Revoking one does not affect machines that already enrolled with it.">
      {active.length ? <table className="ops-mini"><tbody>{active.map(k => <tr key={k.id}>
        <td>{k.label || 'Enrollment key'}</td><td className="ops-muted">created {timeAgo(k.created_at)}{k.expires_at ? `, valid until ${clock(k.expires_at)}` : ''}</td>
        <td className="ops-muted">{k.uses} machine{k.uses === 1 ? '' : 's'} enrolled{k.max_uses ? ` (limit ${k.max_uses})` : ''}</td>
        <td><button className="ops-text-button" onClick={() => revoke(k.id)}>Revoke</button></td></tr>)}</tbody></table> : <p className="ops-muted">No valid keys.</p>}
    </Section>
    <p className="ops-muted"><button className="ops-text-button" onClick={onManual}>Register a machine by hand instead</button> (you get a key for that one machine and type its name now).</p>
  </Drawer>;
}

function WaitingMachines({ machines, api, reload, onApproved }: { machines: any[]; api: Props['api']; reload: Props['reload']; onApproved: (name: string) => void }) {
  const [names, setNames] = useState<Record<string, string>>({});
  const [batches, setBatches] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');

  async function approve(w: any) {
    const name = (names[w.id] ?? w.name).trim();
    setBusy(w.id); setError('');
    try {
      await api(`/workers/${w.id}/approve`, 'POST', { name, capacity: Number(batches[w.id] ?? 5) });
      onApproved(name);
      await reload();
    } catch (err) { setError((err as Error).message); } finally { setBusy(''); }
  }
  async function reject(w: any) {
    if (!window.confirm(`Remove ${w.enrolled_hostname || w.name}? If this PC is not yours, this stops it for good.`)) return;
    setBusy(w.id); setError('');
    try { await api(`/workers/${w.id}`, 'PATCH', { status: 'retired', capacity: w.capacity }); await reload(); }
    catch (err) { setError((err as Error).message); } finally { setBusy(''); }
  }

  return <section className="ops-card ops-waiting">
    <h2>New machines waiting for you</h2>
    <p className="ops-muted">These PCs connected with an enrollment key. They do nothing until you approve them. Give each one a name your team will recognise.</p>
    {error && <div className="ops-alert bad" role="alert">{error}</div>}
    {machines.map(w => <div className="ops-waiting-row" key={w.id}>
      <div className="ops-waiting-who"><strong>{w.enrolled_hostname || w.name}</strong>
        <div className="ops-muted">Found {timeAgo(w.created_at)}. Last seen {timeAgo(w.last_contact_at)}.</div></div>
      <label className="ops-field">Name
        <input value={names[w.id] ?? w.name} maxLength={80} onChange={e => setNames({ ...names, [w.id]: e.target.value })} /></label>
      <label className="ops-field ops-field-small">Trademarks per batch
        <input type="number" min={1} max={100} value={batches[w.id] ?? '5'} onChange={e => setBatches({ ...batches, [w.id]: e.target.value })} /></label>
      <div className="ops-waiting-actions">
        <button className="ops-button" disabled={busy === w.id || !(names[w.id] ?? w.name).trim()} onClick={() => approve(w)}>Approve</button>
        <button className="ops-button danger" disabled={busy === w.id} onClick={() => reject(w)}>Remove</button>
      </div>
    </div>)}
  </section>;
}

function ManualRegister({ api, reload, goTo, onClose, onBack }: { api: Props['api']; reload: Props['reload']; goTo: Props['goTo']; onClose: () => void; onBack: () => void }) {
  const [name, setName] = useState('');
  const [batch, setBatch] = useState('5');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [created, setCreated] = useState<{ name: string; token: string; batch: number } | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true); setError('');
    try {
      const response = await api('/workers', 'POST', { name: name.trim(), capacity: Number(batch) });
      setCreated({ name: response.name, token: response.token, batch: Number(batch) });
      await reload();
    } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  }

  return <Drawer title={created ? `${created.name} is added` : 'Add a machine'} subtitle={created ? 'Finish setting it up in three steps' : 'Register a PC that will scrape the portal'} onClose={onClose}>
    {!created ? <form className="ops-stack" onSubmit={submit}>
      {error && <div className="ops-alert bad" role="alert">{error}</div>}
      <label className="ops-field">Machine name
        <input value={name} onChange={e => setName(e.target.value)} placeholder="For example: Office PC 2" maxLength={80} required autoFocus />
        <span className="ops-muted">A name you will recognise. It must be different from your other machines.</span>
      </label>
      <label className="ops-field">Trademarks per batch
        <input type="number" min={1} max={100} value={batch} onChange={e => setBatch(e.target.value)} required />
        <span className="ops-muted">How many trademarks this machine takes at one time. 5 to 40 works well. You can change it later.</span>
      </label>
      <button className="ops-button" disabled={busy || !name.trim()}>{busy ? 'Adding…' : 'Add machine'}</button>
      <button type="button" className="ops-text-button" onClick={onBack}>Use an enrollment key instead</button>
    </form> : <ol className="ops-steps">
      <li>
        <strong>Save this key now.</strong>
        <p className="ops-muted">For security it is shown only this once. Copy these settings into a file called <code>worker.env</code> on the PC, and change the last line to the name of that PC's VPN file (without .conf).</p>
        <CopyBox text={envFile(created.token, created.batch)} label="Copy settings" />
      </li>
      <li>
        <strong>Give it a portal account.</strong>
        <p className="ops-muted">The machine logs in to the portal with an account assigned to it. Open Portal accounts and assign one to {created.name}.</p>
        <button className="ops-button secondary" onClick={() => { onClose(); goTo('accounts'); }}>Go to Portal accounts</button>
      </li>
      <li>
        <strong>Start the worker on that PC.</strong>
        <p className="ops-muted">Open PowerShell as administrator in the TM Tracker folder and run this. The machine appears as Online here within a minute.</p>
        <CopyBox text={'.\\run-all.ps1 -Mode worker -ConfigFile .\\worker.env'} />
      </li>
    </ol>}
  </Drawer>;
}

function MachineDetails({ id, row, api, reload, goTo, onClose }: { id: string; row: any; api: Props['api']; reload: Props['reload']; goTo: Props['goTo']; onClose: () => void }) {
  const [summary, setSummary] = useState<any>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [batch, setBatch] = useState('');
  const [newKey, setNewKey] = useState('');

  const load = useCallback(async () => {
    try { setSummary(await api(`/workers/${id}/summary`)); setError(''); } catch (err) { setError((err as Error).message); }
  }, [api, id]);
  useEffect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t); }, [load]);

  const w = summary?.worker || row;
  const meta = w?.metadata || {};
  const s = STATUS[w?.status] || { label: w?.status, tone: '' as const, hint: '' };
  const c = w ? connection(w) : { tone: '' as const, text: '' };
  useEffect(() => { if (w && batch === '') setBatch(String(w.capacity)); }, [w, batch]);

  async function run(label: string, path: string, method: string, body: any, confirmText?: string) {
    if (confirmText && !window.confirm(confirmText)) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const response = await api(path, method, body);
      if (response.token) setNewKey(response.token);
      setNotice(label);
      await Promise.all([load(), reload()]);
    } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  }
  const setStatus = (status: string, label: string, confirmText?: string) => run(label, `/workers/${id}`, 'PATCH', { status, capacity: w.capacity }, confirmText);

  const done = Object.entries(summary?.result_totals || {}).filter(([k]) => k !== 'obsolete').reduce((n, [, v]) => n + Number(v), 0);

  return <Drawer title={w?.name || 'Machine'} subtitle={w && <><Tone tone={s.tone}>{s.label}</Tone> <Tone tone={c.tone}>{c.text}</Tone></>} onClose={onClose}>
    {error && <div className="ops-alert bad" role="alert">{error}</div>}
    {notice && <div className="ops-alert good" role="status">{notice}</div>}
    {newKey && <div className="ops-alert"><strong>New key for this machine. Shown only once.</strong>
      <p className="ops-muted">On that PC, replace WORKER_TOKEN in worker.env with this line and restart the worker. The old key has stopped working.</p>
      <CopyBox text={`WORKER_TOKEN=${newKey}`} label="Copy" /></div>}
    {w && <>
      <Section title="What it is doing">
        <p>{w.status === 'retired' ? 'This machine has been removed and can no longer connect.' : `${PHASE[w.phase] || w.phase}. Last seen ${timeAgo(w.last_contact_at)}.`}</p>
        {w.status !== 'retired' && <p className="ops-muted">{s.hint}.</p>}
      </Section>
      <Section title="At a glance">
        <Facts items={[
          ['Trademarks per batch', w.capacity],
          ['Portal accounts', summary ? (summary.accounts.length ? summary.accounts.map((a: any) => <div key={a.id}>{a.email}{!a.enabled && <span className="ops-muted"> (disabled)</span>}</div>) : <span>None assigned. <button className="ops-text-button" onClick={() => { onClose(); goTo('accounts'); }}>Assign one</button></span>) : '…'],
          ['Holding right now', summary ? `${summary.jobs.reserved || 0} trademark(s)` : '…'],
          ['Waiting to retry', summary ? `${summary.jobs.retry_wait || 0} trademark(s)` : '…'],
          ['Results delivered', summary ? `${done} in total` : '…'],
          ['Results waiting to send', meta.counts?.upload_pending ?? '—'],
          ['VPN in use', meta.selected_vpn || '—'],
          ['Free disk space', bytes(meta.free_bytes)],
        ]} />
      </Section>
      <Section title="Recent results">
        {summary?.recent_results?.length ? <table className="ops-mini"><tbody>{summary.recent_results.map((r: any, i: number) => <tr key={i}><td>{r.app_no}</td><td>{OUTCOME[r.disposition] || r.disposition}</td><td className="ops-muted">{timeAgo(r.received_at)}</td></tr>)}</tbody></table> : <p className="ops-muted">Nothing delivered yet.</p>}
      </Section>
      <Section title="Recent problems" hint="Failed attempts are retried automatically.">
        {summary?.recent_problems?.length ? <table className="ops-mini"><tbody>{summary.recent_problems.map((p: any, i: number) => <tr key={i}><td>{p.app_no}</td><td>{p.last_error}</td><td className="ops-muted">{p.state === 'failed' ? 'gave up' : 'will retry'}</td></tr>)}</tbody></table> : <p className="ops-muted">No problems recorded.</p>}
        {meta.last_error && <p className="ops-muted">Machine reported: {meta.last_error}</p>}
      </Section>
      {w.status !== 'retired' && <Section title="Manage">
        {w.status === 'active' ? <>
          <ActionRow title="Pause" text="Stops giving this machine new work. Anything it already scraped is still delivered."><button className="ops-button secondary" disabled={busy} onClick={() => setStatus('paused', 'Paused. No new work will be given.')}>Pause</button></ActionRow>
          <ActionRow title="Finish and stop" text="Lets it finish its current batch, then stops. Use this before moving its portal accounts to another machine."><button className="ops-button secondary" disabled={busy} onClick={() => setStatus('draining', 'It will finish its batch and stop.')}>Finish and stop</button></ActionRow>
        </> : <ActionRow title="Resume" text="Starts giving this machine work again."><button className="ops-button" disabled={busy} onClick={() => setStatus('active', 'Resumed.')}>Resume</button></ActionRow>}
        <ActionRow title="Trademarks per batch" text="How many trademarks it takes at one time.">
          <span className="ops-inline"><input className="ops-days-input" type="number" min={1} max={100} value={batch} onChange={e => setBatch(e.target.value)} />
            <button className="ops-button secondary" disabled={busy || batch === String(w.capacity)} onClick={() => run('Batch size saved.', `/workers/${id}`, 'PATCH', { status: w.status, capacity: Number(batch) })}>Save</button></span>
        </ActionRow>
        <ActionRow title="New key" text="Makes a new key for this machine. The old key stops working right away, so the machine must be updated."><button className="ops-button secondary" disabled={busy} onClick={() => run('New key created.', `/workers/${id}/rotate-token`, 'POST', {}, `Create a new key for ${w.name}? Its current key stops working immediately.`)}>Create new key</button></ActionRow>
        <ActionRow title="Remove machine" text="Disconnects it for good. To use that PC again you would add it as a new machine."><button className="ops-button danger" disabled={busy} onClick={() => setStatus('retired', 'Machine removed.', `Remove ${w.name}? It will be disconnected permanently.`)}>Remove</button></ActionRow>
      </Section>}
      <details className="ops-tech"><summary>Technical details</summary><pre>{JSON.stringify(summary?.worker || row, null, 2)}</pre></details>
    </>}
  </Drawer>;
}
