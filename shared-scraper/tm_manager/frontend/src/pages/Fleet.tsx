import { useEffect, useState, useCallback } from 'react';
import { API_BASE } from '../lib/api';

// Fleet dashboard — Contabo-coordinator + local-worker-fleet migration.
// Read-only in Phase 1 except for account assignment / replan / machine
// status, which only rewrite otp_email_pool.assigned_machine_id and
// fleet_machines rows — nothing here talks to the scraper directly, and
// nothing here affects Contabo's own live-search scraping (Word Mark +
// Class search, TM Search, Auto Discover), which is untouched by this
// migration. No worker machine actually claims through fleet_assignments
// until Phase 2, so "today's progress" will show zero/empty until then.

function OnlinePill({ isOnline }: { isOnline: boolean }) {
  return (
    <span className={`pill ${isOnline ? 's-good' : 's-default'}`}>
      <span className="dot" />{isOnline ? 'Online' : 'Offline'}
    </span>
  );
}

function StatusPill({ status }: { status: string }) {
  const cls = status === 'active' ? 's-good' : 's-default';
  return <span className={`pill ${cls}`}><span className="dot" />{status === 'active' ? 'Active' : 'Disabled'}</span>;
}

export default function Fleet() {
  const [data, setData] = useState<any>(null);
  const [replanning, setReplanning] = useState(false);
  const [replanResult, setReplanResult] = useState<string>('');
  const [error, setError] = useState('');
  const [assigning, setAssigning] = useState<number | null>(null);

  const load = useCallback(() => {
    fetch(`${API_BASE}/api/fleet-status`)
      .then((res) => res.json())
      .then((json) => setData(json))
      .catch(() => setError('Could not load fleet status.'));
  }, []);

  useEffect(() => { load(); }, [load]);

  const replan = async () => {
    setReplanning(true);
    setReplanResult('');
    try {
      const res = await fetch(`${API_BASE}/api/fleet-status/replan`, { method: 'POST' });
      const json = await res.json();
      setReplanResult(`Assigned ${json.assigned} of ${json.due} due, across ${json.machines} machine(s).`);
      load();
    } catch {
      setReplanResult('Replan failed — see server log.');
    } finally {
      setReplanning(false);
    }
  };

  const toggleMachineStatus = async (machineId: number, current: string) => {
    const next = current === 'active' ? 'disabled' : 'active';
    await fetch(`${API_BASE}/api/fleet-status/machines/${machineId}/status`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: next }),
    });
    load();
  };

  const assignAccount = async (accountId: number, machineId: number | null) => {
    setAssigning(accountId);
    try {
      await fetch(`${API_BASE}/api/fleet-status/accounts/${accountId}/assign`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ machine_id: machineId }),
      });
      load();
    } finally {
      setAssigning(null);
    }
  };

  if (error) return <div className="p-7.5 text-[13px] text-status-bad">{error}</div>;
  if (!data) return <div className="p-7.5 text-[13px] text-muted-foreground">Loading…</div>;

  return (
    <div className="flex-1 min-h-0 overflow-y-auto px-7.5 pt-6 pb-15">
      <div className="flex items-center justify-between gap-3 mb-5 flex-wrap">
        <div>
          <h1 className="text-[19px] font-semibold m-0">Fleet</h1>
          <p className="text-muted-foreground text-[13px] mt-0.5">
            Local worker machines handling the daily portfolio re-fetch — separate from this server's own live searches.
          </p>
        </div>
        <div className="flex items-center gap-2.5">
          {replanResult && <span className="text-[12.5px] text-muted-foreground">{replanResult}</span>}
          <button type="button" className="btn btn-accent" onClick={replan} disabled={replanning}>
            {replanning ? 'Replanning…' : 'Replan now'}
          </button>
        </div>
      </div>

      {!data.coordinator_enabled && (
        <div className="max-w-[720px] mb-4 rounded-lg px-4 py-3 text-[12.5px] bg-status-warn-bg text-status-warn">
          Fleet coordinator is disabled on this server (FLEET_COORDINATOR_ENABLED is off) — the daily planning
          tick will not run automatically, though Replan now still works manually.
        </div>
      )}

      <div className="table-panel mb-6">
        <table className="w-full border-separate border-spacing-0">
          <thead>
            <tr>
              <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Machine</th>
              <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Online</th>
              <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Status</th>
              <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Accounts</th>
              <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Today: pending / done / failed</th>
              <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Last heartbeat</th>
              <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Actions</th>
            </tr>
          </thead>
          <tbody>
            {data.machines.length === 0 && (
              <tr><td colSpan={7} className="px-2.5 py-6 text-center text-[13px] text-muted-foreground">No worker machines have registered yet.</td></tr>
            )}
            {data.machines.map((m: any) => (
              <tr key={m.id}>
                <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2 text-[13px] font-medium">{m.display_name || m.machine_key}</td>
                <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2"><OnlinePill isOnline={m.is_online} /></td>
                <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2"><StatusPill status={m.status} /></td>
                <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2 text-[13px]">{m.account_count}</td>
                <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2 text-[13px] font-mono">{m.today_pending} / {m.today_done} / {m.today_failed}</td>
                <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2 text-[12.5px] text-muted-foreground font-mono">{m.last_heartbeat_at || '—'}</td>
                <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2">
                  <button type="button" className="btn-ghost text-[12px] px-2 py-1 rounded" onClick={() => toggleMachineStatus(m.id, m.status)}>
                    {m.status === 'active' ? 'Disable' : 'Enable'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h2 className="text-[15px] font-semibold mb-2">Account assignment</h2>
      <p className="text-[12.5px] text-muted-foreground mb-3 max-w-[640px]">
        Only cold-pool accounts are assignable here — accounts reserved for this server's own warm/word-mark
        live search never appear in this list and can never be handed to a worker machine.
      </p>
      <div className="table-panel max-w-[640px]">
        <table className="w-full border-separate border-spacing-0">
          <thead>
            <tr>
              <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Account</th>
              <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Assigned to</th>
              <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Action</th>
            </tr>
          </thead>
          <tbody>
            {[...data.assigned_accounts, ...data.unassigned_accounts].map((a: any) => (
              <tr key={a.id}>
                <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2 text-[13px] font-mono">{a.email_address}</td>
                <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2 text-[13px]">
                  {a.assigned_machine_id
                    ? (data.machines.find((m: any) => m.id === a.assigned_machine_id)?.display_name
                       || data.machines.find((m: any) => m.id === a.assigned_machine_id)?.machine_key
                       || `#${a.assigned_machine_id}`)
                    : <span className="text-muted-foreground">Unassigned</span>}
                </td>
                <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2">
                  <select
                    className="border border-border-strong rounded-md px-2 py-1 text-[12.5px]"
                    value={a.assigned_machine_id ?? ''}
                    disabled={assigning === a.id}
                    onChange={(e) => assignAccount(a.id, e.target.value ? Number(e.target.value) : null)}
                  >
                    <option value="">Unassigned</option>
                    {data.machines.map((m: any) => (
                      <option key={m.id} value={m.id}>{m.display_name || m.machine_key}</option>
                    ))}
                  </select>
                </td>
              </tr>
            ))}
            {data.assigned_accounts.length === 0 && data.unassigned_accounts.length === 0 && (
              <tr><td colSpan={3} className="px-2.5 py-6 text-center text-[13px] text-muted-foreground">No cold-pool accounts available to assign.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
