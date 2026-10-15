import { useCallback, useEffect, useMemo, useState } from 'react';
import { ExternalLink, Plus } from 'lucide-react';
import { ActionRow, Drawer, Facts, PageIntro, Section, timeAgo, Tone } from './ui';

type Props = {
  api: (path: string, method?: string, body?: any) => Promise<any>;
  goTo: (page: string) => void;
};

const MACHINE_STATUS: Record<string, string> = { active: 'Active', paused: 'Paused', draining: 'Finishing up' };

function machineLabel(m: any): string {
  return `${m.name} — ${MACHINE_STATUS[m.status] || m.status}, ${m.accounts} account${m.accounts === 1 ? '' : 's'}`;
}

function accountState(a: any): { tone: 'good' | 'bad' | ''; text: string } {
  if (!a.enabled) return { tone: 'bad', text: 'Disabled' };
  return a.worker_id ? { tone: 'good', text: 'Assigned' } : { tone: '', text: 'Available' };
}

export default function PortalAccounts({ api, goTo }: Props) {
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [problems, setProblems] = useState<{ email: string; reason: string }[]>([]);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState('all');
  const [target, setTarget] = useState('');
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try { setData(await api('/accounts-overview')); setError(''); } catch (err) { setError((err as Error).message); }
  }, [api]);
  useEffect(() => { load(); const t = setInterval(load, 10000); return () => clearInterval(t); }, [load]);

  const accounts: any[] = data?.accounts || [];
  const machines: any[] = data?.machines || [];
  const byId = useMemo(() => Object.fromEntries(accounts.map(a => [a.id, a])), [accounts]);

  const shown = accounts.filter(a => filter === 'all' ? true : filter === 'unassigned' ? !a.worker_id : filter === 'disabled' ? !a.enabled : a.worker_id === filter);
  const allShownPicked = shown.length > 0 && shown.every(a => picked.has(a.id));

  async function bulk(label: string, body: any) {
    setBusy(true); setError(''); setNotice(''); setProblems([]);
    try {
      const result = await api('/accounts/assign', 'POST', body);
      const ok = result.assigned.length;
      if (ok) setNotice(`${label}: ${ok} account${ok === 1 ? '' : 's'}.`);
      setProblems(result.failed.map((f: any) => ({ email: byId[f.id]?.email || 'An account', reason: f.reason })));
      setPicked(new Set(result.failed.map((f: any) => f.id)));
      await load();
    } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  }
  const ids = Array.from(picked);
  const chosenMachine = machines.find(m => m.id === target);

  const chips: [string, string][] = [['all', `All (${data?.total ?? 0})`], ['unassigned', `Not assigned (${data?.unassigned ?? 0})`],
    ...machines.filter(m => m.accounts > 0).map(m => [m.id, `${m.name} (${m.accounts})`] as [string, string]),
    ...(data?.disabled ? [['disabled', `Disabled (${data.disabled})`] as [string, string]] : [])];

  return <>
    <PageIntro action={<button className="ops-button" onClick={() => setAdding(true)}><Plus size={15} />Add an account</button>}>
      A portal account is a Gmail inbox plus an app password. A machine uses it to log in to the IP India portal and read the
      sign-in code that the portal emails. Each account works on one machine at a time. A machine can use several accounts at once,
      which makes it scrape faster, so give each machine a few.
    </PageIntro>
    {error && <div className="ops-alert bad" role="alert">{error}</div>}
    {notice && <div className="ops-alert good" role="status">{notice}</div>}
    {problems.length > 0 && <div className="ops-alert bad" role="alert"><strong>Some accounts could not be changed:</strong>
      <ul className="ops-list">{problems.map((p, i) => <li key={i}><b>{p.email}</b>: {p.reason}</li>)}</ul>
      <button className="ops-text-button" onClick={() => goTo('workers')}>Open Fleet machines</button></div>}

    <section className="ops-card ops-table-card">
      <div className="ops-card-heading">
        <h2>Portal accounts</h2>
        <span className="ops-muted">{data ? `${data.total} accounts · ${data.assigned} assigned · ${data.unassigned} not assigned${data.disabled ? ` · ${data.disabled} disabled` : ''}` : 'Loading…'}</span>
      </div>
      <div className="ops-chips">{chips.map(([key, label]) => <button key={key} className={`ops-chip ${filter === key ? 'on' : ''}`} onClick={() => setFilter(key)}>{label}</button>)}</div>
      {picked.size > 0 && <div className="ops-bulkbar">
        <strong>{picked.size} selected</strong>
        <select value={target} onChange={e => setTarget(e.target.value)} aria-label="Machine to give the accounts to">
          <option value="">Choose a machine…</option>
          {machines.map(m => <option key={m.id} value={m.id}>{machineLabel(m)}</option>)}
        </select>
        <button className="ops-button" disabled={busy || !target} onClick={() => bulk(`Assigned to ${chosenMachine?.name}`, { account_ids: ids, worker_id: target })}>Assign</button>
        <button className="ops-button secondary" disabled={busy} onClick={() => bulk('Taken back', { account_ids: ids, worker_id: null })}>Take back</button>
        <button className="ops-button secondary" disabled={busy} onClick={() => bulk('Enabled', { account_ids: ids, enabled: true })}>Enable</button>
        <button className="ops-button secondary" disabled={busy} onClick={() => bulk('Disabled', { account_ids: ids, enabled: false })}>Disable</button>
        <button className="ops-text-button" onClick={() => setPicked(new Set())}>Clear</button>
      </div>}
      <div className="ops-table-wrap"><table>
        <thead><tr>
          <th className="ops-check"><input type="checkbox" aria-label="Select all shown" checked={allShownPicked} onChange={() => setPicked(allShownPicked ? new Set() : new Set(shown.map(a => a.id)))} /></th>
          <th>Account</th><th>Machine</th><th>Status</th><th>Added</th><th />
        </tr></thead>
        <tbody>{shown.map(a => {
          const st = accountState(a);
          return <tr key={a.id} className="ops-clickable" onClick={() => setOpenId(a.id)}>
            <td className="ops-check" onClick={e => e.stopPropagation()}><input type="checkbox" aria-label={`Select ${a.email}`} checked={picked.has(a.id)} onChange={() => { const next = new Set(picked); next.has(a.id) ? next.delete(a.id) : next.add(a.id); setPicked(next); }} /></td>
            <td><strong>{a.email}</strong></td>
            <td>{a.worker_name || <span className="ops-muted">Not assigned</span>}</td>
            <td><Tone tone={st.tone}>{st.text}</Tone></td>
            <td className="ops-muted">{timeAgo(a.created_at)}</td>
            <td><button className="ops-button secondary" onClick={e => { e.stopPropagation(); setOpenId(a.id); }}>Manage</button></td>
          </tr>;
        })}</tbody>
      </table>
      {!shown.length && <div className="ops-empty"><h3>{accounts.length ? 'No accounts in this view' : 'No portal accounts yet'}</h3><p>{accounts.length ? 'Try a different filter above.' : 'Add the Gmail accounts your machines will use to log in to the portal.'}</p></div>}
      </div>
    </section>
    {adding && <AddAccount api={api} machines={machines} goTo={goTo} reload={load} onClose={() => setAdding(false)} />}
    {openId && byId[openId] && <AccountDetails account={byId[openId]} machines={machines} api={api} reload={load} goTo={goTo} onClose={() => setOpenId(null)} />}
  </>;
}

function AddAccount({ api, machines, goTo, reload, onClose }: { api: Props['api']; machines: any[]; goTo: Props['goTo']; reload: () => Promise<void>; onClose: () => void }) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [machine, setMachine] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [added, setAdded] = useState('');

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true); setError('');
    try {
      // Gmail shows app passwords in groups of four; the spaces are not part of the password.
      await api('/accounts', 'POST', { email: email.trim(), app_password: password.replace(/\s+/g, ''), ...(machine ? { worker_id: machine } : {}) });
      setAdded(email.trim()); setEmail(''); setPassword('');
      await reload();
    } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  }

  return <Drawer title="Add an account" subtitle="A Gmail inbox the portal can send its sign-in code to" onClose={onClose}>
    <form className="ops-stack" onSubmit={submit}>
      {added && <div className="ops-alert good" role="status">{added} was added. You can add another one below.</div>}
      {error && <div className="ops-alert bad" role="alert">{error}</div>}
      <label className="ops-field">Gmail address
        <input type="email" value={email} onChange={e => setEmail(e.target.value)} placeholder="name@gmail.com" required autoFocus />
      </label>
      <label className="ops-field">App password
        <input type="password" value={password} onChange={e => setPassword(e.target.value)} autoComplete="off" required />
        <span className="ops-muted">The 16-letter password Google creates for apps, not the normal Gmail password. Spaces are fine. It is stored encrypted and only the machine using the account can read it.</span>
      </label>
      <label className="ops-field">Give it to a machine
        <select value={machine} onChange={e => setMachine(e.target.value)}>
          <option value="">Not yet, I will assign it later</option>
          {machines.map(m => <option key={m.id} value={m.id}>{machineLabel(m)}</option>)}
        </select>
        {!machines.length && <span className="ops-muted">There are no machines yet. <button type="button" className="ops-text-button" onClick={() => { onClose(); goTo('workers'); }}>Add a machine first</button></span>}
      </label>
      <button className="ops-button" disabled={busy || !email.trim() || !password}>{busy ? 'Adding…' : 'Add account'}</button>
      <details className="ops-tech">
        <summary>How do I get an app password?</summary>
        <ol className="ops-help-steps">
          <li>Sign in to the Google account and turn on <b>2-Step Verification</b> (Security settings).</li>
          <li>In Gmail, open <b>Settings → See all settings → Forwarding and POP/IMAP</b> and choose <b>Enable IMAP</b>.</li>
          <li>Go to <a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noreferrer">myaccount.google.com/apppasswords <ExternalLink size={11} /></a>, create a password named “TM Tracker”, and copy it.</li>
          <li>Paste it above. Use a separate Gmail for each account; two accounts must not share an inbox.</li>
        </ol>
      </details>
    </form>
  </Drawer>;
}

function AccountDetails({ account, machines, api, reload, goTo, onClose }: { account: any; machines: any[]; api: Props['api']; reload: () => Promise<void>; goTo: Props['goTo']; onClose: () => void }) {
  const [machine, setMachine] = useState(account.worker_id || '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [blocked, setBlocked] = useState(false);
  const st = accountState(account);

  async function change(label: string, body: any) {
    setBusy(true); setError(''); setNotice(''); setBlocked(false);
    try {
      const result = await api('/accounts/assign', 'POST', { account_ids: [account.id], ...body });
      if (result.failed.length) { setError(result.failed[0].reason); setBlocked(true); } else setNotice(label);
      await reload();
    } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  }

  return <Drawer title={account.email} subtitle={<><Tone tone={st.tone}>{st.text}</Tone></>} onClose={onClose}>
    {error && <div className="ops-alert bad" role="alert">{error}{blocked && <div><button className="ops-text-button" onClick={() => { onClose(); goTo('workers'); }}>Open Fleet machines</button></div>}</div>}
    {notice && <div className="ops-alert good" role="status">{notice}</div>}
    <Section title="Where it is used">
      <Facts items={[['Machine', account.worker_name || 'Not assigned'], ['Added', timeAgo(account.created_at)]]} />
    </Section>
    <Section title="Manage">
      <ActionRow title="Machine" text="The machine that logs in with this account. Only one machine can use it at a time.">
        <span className="ops-inline">
          <select value={machine} onChange={e => setMachine(e.target.value)} aria-label="Machine">
            <option value="">Not assigned</option>
            {machines.map(m => <option key={m.id} value={m.id}>{machineLabel(m)}</option>)}
          </select>
          <button className="ops-button secondary" disabled={busy || machine === (account.worker_id || '')} onClick={() => change(machine ? 'Assigned.' : 'Taken back.', { worker_id: machine || null })}>Save</button>
        </span>
      </ActionRow>
      {account.enabled
        ? <ActionRow title="Disable" text="Stops this account being used. Its machine will not log in with it. You can enable it again any time."><button className="ops-button secondary" disabled={busy} onClick={() => change('Disabled.', { enabled: false })}>Disable</button></ActionRow>
        : <ActionRow title="Enable" text="Lets a machine use this account again."><button className="ops-button" disabled={busy} onClick={() => change('Enabled.', { enabled: true })}>Enable</button></ActionRow>}
    </Section>
    <details className="ops-tech"><summary>Technical details</summary><pre>{JSON.stringify(account, null, 2)}</pre></details>
  </Drawer>;
}
