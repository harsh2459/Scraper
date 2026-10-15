import { useCallback, useEffect, useState } from 'react';
import { Pencil, X, Check } from 'lucide-react';
import Ring from '../components/tm/Ring';
import SearchSelect from '../components/SearchSelect';
import { API_BASE } from '../lib/api';

interface Plan {
  id: number;
  name: string;
  tm_limit: number;
  seat_limit: number;
  is_active: boolean;
}

interface UserOption {
  id: number;
  email: string;
  display_name: string | null;
}

interface Subscription {
  id: number;
  user_id: number;
  plan_id: number;
  plan_name: string;
  plan_tm_limit: number;
  plan_seat_limit: number;
  status: string;
  renews_at: string | null;
  tm_limit_override: number | null;
  seat_limit_override: number | null;
  search_limit_override: number | null;
  user_email: string;
  user_display_name: string | null;
  tm_count: number;
}

export default function ManageSubscriptions() {
  const [subs, setSubs] = useState<Subscription[] | null>(null);
  const [plans, setPlans] = useState<Plan[]>([]);
  const [users, setUsers] = useState<UserOption[]>([]);
  const [error, setError] = useState('');

  const [editingId, setEditingId] = useState<number | null>(null);
  const [editPlanId, setEditPlanId] = useState('');
  const [editStatus, setEditStatus] = useState('active');
  const [editRenewsAt, setEditRenewsAt] = useState('');
  const [editTmOverride, setEditTmOverride] = useState('');
  const [editSeatOverride, setEditSeatOverride] = useState('');
  const [editSearchOverride, setEditSearchOverride] = useState('');
  const [saving, setSaving] = useState(false);
  const [editError, setEditError] = useState('');

  // "Assign new" mini-form — a subscription for a user not in the list yet
  const [newUserId, setNewUserId] = useState('');
  const [newPlanId, setNewPlanId] = useState('');
  const [assigning, setAssigning] = useState(false);
  const [assignError, setAssignError] = useState('');

  const load = useCallback(async () => {
    try {
      const [subsRes, plansRes, usersRes] = await Promise.all([
        fetch(`${API_BASE}/api/admin/subscriptions`),
        fetch(`${API_BASE}/api/admin/plans`),
        fetch(`${API_BASE}/api/users`),
      ]);
      const subsJson = await subsRes.json();
      const plansJson = await plansRes.json();
      const usersJson = await usersRes.json();
      if (!subsRes.ok) {
        setError(subsJson.error || 'Could not load subscriptions.');
        return;
      }
      setSubs(subsJson.subscriptions);
      setPlans(plansJson.plans || []);
      setUsers(usersJson.users || []);
    } catch {
      setError('Could not reach the server.');
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const startEdit = (s: Subscription) => {
    setEditingId(s.id);
    setEditPlanId(String(s.plan_id));
    setEditStatus(s.status);
    setEditRenewsAt(s.renews_at ? s.renews_at.slice(0, 10) : '');
    setEditTmOverride(s.tm_limit_override != null ? String(s.tm_limit_override) : '');
    setEditSeatOverride(s.seat_limit_override != null ? String(s.seat_limit_override) : '');
    setEditSearchOverride(s.search_limit_override != null ? String(s.search_limit_override) : '');
    setEditError('');
  };

  const cancelEdit = () => {
    setEditingId(null);
    setEditError('');
  };

  const saveEdit = async (s: Subscription) => {
    setSaving(true);
    setEditError('');
    try {
      const res = await fetch(`${API_BASE}/api/admin/subscriptions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          user_id: s.user_id,
          plan_id: parseInt(editPlanId, 10),
          status: editStatus,
          renews_at: editRenewsAt || null,
          tm_limit_override: editTmOverride ? parseInt(editTmOverride, 10) : null,
          seat_limit_override: editSeatOverride ? parseInt(editSeatOverride, 10) : null,
          search_limit_override: editSearchOverride ? parseInt(editSearchOverride, 10) : null,
        }),
      });
      const json = await res.json();
      if (!res.ok) {
        setEditError(json.error || 'Could not save changes.');
        return;
      }
      setEditingId(null);
      load();
    } catch {
      setEditError('Could not reach the server.');
    } finally {
      setSaving(false);
    }
  };

  const assignNew = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newUserId) { setAssignError('Choose a user first.'); return; }
    setAssigning(true);
    setAssignError('');
    try {
      const res = await fetch(`${API_BASE}/api/admin/subscriptions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: parseInt(newUserId, 10), plan_id: parseInt(newPlanId, 10) }),
      });
      const json = await res.json();
      if (!res.ok) {
        setAssignError(json.error || 'Could not assign plan.');
        return;
      }
      setNewUserId('');
      setNewPlanId('');
      load();
    } catch {
      setAssignError('Could not reach the server.');
    } finally {
      setAssigning(false);
    }
  };

  if (error) {
    return (
      <div className="flex-1 min-h-0 overflow-y-auto px-7.5 pt-6 pb-15">
        <div className="max-w-200 rounded-lg px-4 py-3 text-[12.5px] bg-status-bad-bg text-status-bad">{error}</div>
      </div>
    );
  }

  if (!subs) return <div className="loading"><Ring state="loading" size={28} /><span>Loading subscriptions…</span></div>;

  return (
    <div className="flex-1 min-h-0 overflow-y-auto px-7.5 pt-6 pb-15">
      <div className="mb-5">
        <h1 className="text-[19px] font-semibold m-0">Subscriptions</h1>
        <p className="text-muted-foreground text-[13px] mt-1">
          Every paying account's plan and live usage. A user not listed here has no subscription row and is
          unlimited by default — assign one below to start enforcing limits for them.
        </p>
      </div>

      <form onSubmit={assignNew} className="max-w-200 bg-panel border border-border rounded-xl p-5 mb-6">
        <div className="eyebrow text-muted-foreground mb-3">Assign a plan to a user</div>
        {assignError && (
          <div className="rounded-lg px-3.5 py-2.5 text-[12.5px] mb-3 bg-status-bad-bg text-status-bad">{assignError}</div>
        )}
        <div className="grid grid-cols-[1fr_1fr_auto] gap-3 items-end">
          <div>
            <label className="block eyebrow text-muted-foreground! mb-1.5" htmlFor="new-sub-user">User</label>
            <SearchSelect
              id="new-sub-user"
              value={newUserId}
              onChange={setNewUserId}
              placeholder="Search by name or email…"
              emptyText="No user without a plan matches"
              options={users
                .filter((u) => !subs.some((s) => s.user_id === u.id))
                .map((u) => ({ value: String(u.id), label: u.display_name || u.email, hint: u.display_name ? u.email : undefined }))}
            />
          </div>
          <div>
            <label className="block eyebrow text-muted-foreground! mb-1.5" htmlFor="new-sub-plan">Plan</label>
            <select
              id="new-sub-plan"
              value={newPlanId}
              onChange={(e) => setNewPlanId(e.target.value)}
              required
              className="w-full border border-border-strong rounded-md px-2.5 py-2 text-[13px] outline-none focus:border-primary bg-background"
            >
              <option value="" disabled>Choose a plan…</option>
              {plans.map((p) => (
                <option key={p.id} value={p.id}>{p.name} ({p.tm_limit} TMs)</option>
              ))}
            </select>
          </div>
          <button type="submit" disabled={assigning} className="btn btn-accent">
            {assigning ? 'Assigning…' : 'Assign'}
          </button>
        </div>
      </form>

      <div className="max-w-200 border border-border rounded-xl overflow-hidden">
        {subs.map((s) => (
          editingId === s.id ? (
            <div key={s.id} className="px-4 py-3.5 border-b border-border last:border-b-0 bg-muted/40">
              {editError && (
                <div className="rounded-lg px-3 py-2 text-[12px] mb-2.5 bg-status-bad-bg text-status-bad">{editError}</div>
              )}
              <div className="grid grid-cols-2 gap-2.5 mb-2.5">
                <div>
                  <label className="block eyebrow text-muted-foreground! mb-1" htmlFor={`edit-plan-${s.id}`}>Plan</label>
                  <select
                    id={`edit-plan-${s.id}`}
                    value={editPlanId}
                    onChange={(e) => setEditPlanId(e.target.value)}
                    className="w-full border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary bg-background"
                  >
                    {plans.map((p) => (
                      <option key={p.id} value={p.id}>{p.name} ({p.tm_limit} TMs)</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="block eyebrow text-muted-foreground! mb-1" htmlFor={`edit-status-${s.id}`}>Status</label>
                  <select
                    id={`edit-status-${s.id}`}
                    value={editStatus}
                    onChange={(e) => setEditStatus(e.target.value)}
                    className="w-full border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary bg-background"
                  >
                    <option value="active">active</option>
                    <option value="expired">expired</option>
                    <option value="cancelled">cancelled</option>
                  </select>
                </div>
              </div>
              <div className="grid grid-cols-3 gap-2.5 mb-3">
                <div>
                  <label className="block eyebrow text-muted-foreground! mb-1" htmlFor={`edit-renews-${s.id}`}>Renews at</label>
                  <input
                    id={`edit-renews-${s.id}`}
                    type="date"
                    value={editRenewsAt}
                    onChange={(e) => setEditRenewsAt(e.target.value)}
                    className="w-full border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary"
                  />
                </div>
                <div>
                  <label className="block eyebrow text-muted-foreground! mb-1" htmlFor={`edit-tm-ovr-${s.id}`}>TM override</label>
                  <input
                    id={`edit-tm-ovr-${s.id}`}
                    type="number"
                    min="0"
                    value={editTmOverride}
                    onChange={(e) => setEditTmOverride(e.target.value)}
                    placeholder="plan default"
                    className="w-full border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary"
                  />
                </div>
                <div>
                  <label className="block eyebrow text-muted-foreground! mb-1" htmlFor={`edit-seat-ovr-${s.id}`}>Seat override</label>
                  <input
                    id={`edit-seat-ovr-${s.id}`}
                    type="number"
                    min="0"
                    value={editSeatOverride}
                    onChange={(e) => setEditSeatOverride(e.target.value)}
                    placeholder="plan default"
                    className="w-full border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary"
                  />
                </div>
                <div>
                  <label className="block eyebrow text-muted-foreground! mb-1" htmlFor={`edit-search-ovr-${s.id}`}>Search override</label>
                  <input
                    id={`edit-search-ovr-${s.id}`}
                    type="number"
                    min="0"
                    value={editSearchOverride}
                    onChange={(e) => setEditSearchOverride(e.target.value)}
                    placeholder="plan default"
                    className="w-full border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary"
                  />
                </div>
              </div>
              <div className="flex items-center gap-2">
                <button type="button" onClick={() => saveEdit(s)} disabled={saving} className="btn btn-accent inline-flex items-center gap-1.5 text-[12.5px] px-2.5 py-1.5">
                  <Check size={13} /> {saving ? 'Saving…' : 'Save'}
                </button>
                <button type="button" onClick={cancelEdit} disabled={saving} className="btn-ghost inline-flex items-center gap-1.5 text-[12.5px] px-2.5 py-1.5 rounded-md">
                  <X size={13} /> Cancel
                </button>
              </div>
            </div>
          ) : (
            <div key={s.id} className="flex items-center gap-3 px-4 py-3 border-b border-border last:border-b-0">
              <div className="min-w-0 flex-1">
                <div className="text-[13px] font-medium truncate flex items-center gap-1.5">
                  {s.user_display_name || s.user_email}
                  <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-muted text-muted-foreground">{s.plan_name}</span>
                  {s.status !== 'active' && (
                    <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-status-bad-bg text-status-bad">{s.status}</span>
                  )}
                  {(s.tm_limit_override != null || s.seat_limit_override != null) && (
                    <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-status-good-bg text-status-good">override</span>
                  )}
                </div>
                <div className="text-[11.5px] text-muted-foreground truncate">
                  {s.tm_count} / {s.tm_limit_override ?? s.plan_tm_limit} TMs
                  {s.renews_at && <> · renews {s.renews_at.slice(0, 10)}</>}
                </div>
              </div>
              <div className="flex items-center gap-1.5 flex-shrink-0">
                <button
                  type="button"
                  onClick={() => startEdit(s)}
                  title="Edit plan, status, overrides"
                  className="btn-ghost p-1.5 rounded-md"
                >
                  <Pencil size={15} />
                </button>
              </div>
            </div>
          )
        ))}
        {subs.length === 0 && (
          <div className="px-4 py-6 text-center text-[13px] text-muted-foreground">No subscriptions assigned yet.</div>
        )}
      </div>
    </div>
  );
}
