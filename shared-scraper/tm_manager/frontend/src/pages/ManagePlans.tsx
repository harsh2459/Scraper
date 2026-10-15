import { useCallback, useEffect, useState } from 'react';
import { Plus, Pencil, X, Check } from 'lucide-react';
import Ring from '../components/tm/Ring';
import { API_BASE } from '../lib/api';

interface Plan {
  id: number;
  name: string;
  price_cents: number | null;
  tm_limit: number;
  seat_limit: number;
  search_limit: number | null;
  billing_period_months: number;
  features: Record<string, boolean>;
  is_active: boolean;
}

const BILLING_PERIODS = [
  { value: 1, label: 'Monthly' },
  { value: 3, label: 'Every 3 months' },
  { value: 6, label: 'Every 6 months' },
  { value: 12, label: 'Yearly' },
];

const formatPrice = (cents: number | null) =>
  cents == null ? '—' : `₹${(cents / 100).toLocaleString('en-IN')}/year`;

export default function ManagePlans() {
  const [plans, setPlans] = useState<Plan[] | null>(null);
  const [error, setError] = useState('');

  const [name, setName] = useState('');
  const [priceRupees, setPriceRupees] = useState('');
  const [tmLimit, setTmLimit] = useState('');
  const [seatLimit, setSeatLimit] = useState('1');
  const [searchLimit, setSearchLimit] = useState('');
  const [billingPeriodMonths, setBillingPeriodMonths] = useState('1');
  const [creating, setCreating] = useState(false);
  const [formError, setFormError] = useState('');

  const [editingId, setEditingId] = useState<number | null>(null);
  const [editTmLimit, setEditTmLimit] = useState('');
  const [editSeatLimit, setEditSeatLimit] = useState('');
  const [editSearchLimit, setEditSearchLimit] = useState('');
  const [editBillingPeriodMonths, setEditBillingPeriodMonths] = useState('1');
  const [editPriceRupees, setEditPriceRupees] = useState('');
  const [saving, setSaving] = useState(false);
  const [editError, setEditError] = useState('');

  const load = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/admin/plans`);
      const json = await res.json();
      if (!res.ok) {
        setError(json.error || 'Could not load plans.');
        return;
      }
      setPlans(json.plans);
    } catch {
      setError('Could not reach the server.');
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const createPlan = async (e: React.FormEvent) => {
    e.preventDefault();
    setCreating(true);
    setFormError('');
    try {
      const res = await fetch(`${API_BASE}/api/admin/plans`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name,
          tm_limit: parseInt(tmLimit, 10),
          seat_limit: parseInt(seatLimit, 10),
          search_limit: searchLimit ? parseInt(searchLimit, 10) : null,
          billing_period_months: parseInt(billingPeriodMonths, 10),
          price_cents: priceRupees ? Math.round(parseFloat(priceRupees) * 100) : null,
        }),
      });
      const json = await res.json();
      if (!res.ok) {
        setFormError(json.error || 'Could not create plan.');
        return;
      }
      setName('');
      setPriceRupees('');
      setTmLimit('');
      setSeatLimit('1');
      setSearchLimit('');
      setBillingPeriodMonths('1');
      load();
    } catch {
      setFormError('Could not reach the server.');
    } finally {
      setCreating(false);
    }
  };

  const startEdit = (p: Plan) => {
    setEditingId(p.id);
    setEditTmLimit(String(p.tm_limit));
    setEditSeatLimit(String(p.seat_limit));
    setEditSearchLimit(p.search_limit != null ? String(p.search_limit) : '');
    setEditBillingPeriodMonths(String(p.billing_period_months || 1));
    setEditPriceRupees(p.price_cents != null ? String(p.price_cents / 100) : '');
    setEditError('');
  };

  const cancelEdit = () => {
    setEditingId(null);
    setEditError('');
  };

  const saveEdit = async (p: Plan) => {
    setSaving(true);
    setEditError('');
    try {
      const res = await fetch(`${API_BASE}/api/admin/plans/${p.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          tm_limit: parseInt(editTmLimit, 10),
          seat_limit: parseInt(editSeatLimit, 10),
          search_limit: editSearchLimit ? parseInt(editSearchLimit, 10) : null,
          billing_period_months: parseInt(editBillingPeriodMonths, 10),
          price_cents: editPriceRupees ? Math.round(parseFloat(editPriceRupees) * 100) : null,
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

  const toggleActive = async (p: Plan) => {
    await fetch(`${API_BASE}/api/admin/plans/${p.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ is_active: !p.is_active }),
    });
    load();
  };

  if (error) {
    return (
      <div className="flex-1 min-h-0 overflow-y-auto px-7.5 pt-6 pb-15">
        <div className="max-w-160 rounded-lg px-4 py-3 text-[12.5px] bg-status-bad-bg text-status-bad">{error}</div>
      </div>
    );
  }

  if (!plans) return <div className="loading"><Ring state="loading" size={28} /><span>Loading plans…</span></div>;

  return (
    <div className="flex-1 min-h-0 overflow-y-auto px-7.5 pt-6 pb-15">
      <div className="mb-5">
        <h1 className="text-[19px] font-semibold m-0">Manage plans</h1>
        <p className="text-muted-foreground text-[13px] mt-1">
          These are the subscription tiers customers can be assigned to — edit limits here without a redeploy.
          Per-customer overrides (e.g. bumping one account's limit above its plan) are set on the
          Subscriptions page, not here.
        </p>
      </div>

      <form onSubmit={createPlan} className="max-w-160 bg-panel border border-border rounded-xl p-5 mb-6">
        <div className="eyebrow text-muted-foreground mb-3">Add a new plan</div>
        {formError && (
          <div className="rounded-lg px-3.5 py-2.5 text-[12.5px] mb-3 bg-status-bad-bg text-status-bad">{formError}</div>
        )}
        <div className="grid grid-cols-2 gap-3 mb-3">
          <div>
            <label className="block eyebrow text-muted-foreground! mb-1.5" htmlFor="new-plan-name">Name</label>
            <input
              id="new-plan-name"
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
              placeholder="Starter"
              className="w-full border border-border-strong rounded-md px-2.5 py-2 text-[13px] outline-none focus:border-primary"
            />
          </div>
          <div>
            <label className="block eyebrow text-muted-foreground! mb-1.5" htmlFor="new-plan-price">Price (₹/year, optional)</label>
            <input
              id="new-plan-price"
              type="number"
              min="0"
              value={priceRupees}
              onChange={(e) => setPriceRupees(e.target.value)}
              placeholder="4999"
              className="w-full border border-border-strong rounded-md px-2.5 py-2 text-[13px] outline-none focus:border-primary"
            />
          </div>
        </div>
        <div className="grid grid-cols-2 gap-3 mb-4">
          <div>
            <label className="block eyebrow text-muted-foreground! mb-1.5" htmlFor="new-plan-tm">TM limit</label>
            <input
              id="new-plan-tm"
              type="number"
              min="0"
              value={tmLimit}
              onChange={(e) => setTmLimit(e.target.value)}
              required
              placeholder="250"
              className="w-full border border-border-strong rounded-md px-2.5 py-2 text-[13px] outline-none focus:border-primary"
            />
          </div>
          <div>
            <label className="block eyebrow text-muted-foreground! mb-1.5" htmlFor="new-plan-seats">Seat limit</label>
            <input
              id="new-plan-seats"
              type="number"
              min="1"
              value={seatLimit}
              onChange={(e) => setSeatLimit(e.target.value)}
              required
              className="w-full border border-border-strong rounded-md px-2.5 py-2 text-[13px] outline-none focus:border-primary"
            />
          </div>
        </div>
        <div className="grid grid-cols-2 gap-3 mb-4">
          <div>
            <label className="block eyebrow text-muted-foreground! mb-1.5" htmlFor="new-plan-search">TM Search limit (blank = unlimited)</label>
            <input
              id="new-plan-search"
              type="number"
              min="0"
              value={searchLimit}
              onChange={(e) => setSearchLimit(e.target.value)}
              placeholder="200"
              className="w-full border border-border-strong rounded-md px-2.5 py-2 text-[13px] outline-none focus:border-primary"
            />
          </div>
          <div>
            <label className="block eyebrow text-muted-foreground! mb-1.5" htmlFor="new-plan-period">Search limit resets</label>
            <select
              id="new-plan-period"
              value={billingPeriodMonths}
              onChange={(e) => setBillingPeriodMonths(e.target.value)}
              className="w-full border border-border-strong rounded-md px-2.5 py-2 text-[13px] outline-none focus:border-primary"
            >
              {BILLING_PERIODS.map((p) => (
                <option key={p.value} value={p.value}>{p.label}</option>
              ))}
            </select>
          </div>
        </div>
        <button type="submit" disabled={creating} className="btn btn-accent inline-flex items-center gap-1.5">
          <Plus size={14} /> {creating ? 'Creating…' : 'Create plan'}
        </button>
      </form>

      <div className="max-w-160 border border-border rounded-xl overflow-hidden">
        {plans.map((p) => (
          editingId === p.id ? (
            <div key={p.id} className="px-4 py-3.5 border-b border-border last:border-b-0 bg-muted/40">
              {editError && (
                <div className="rounded-lg px-3 py-2 text-[12px] mb-2.5 bg-status-bad-bg text-status-bad">{editError}</div>
              )}
              <div className="grid grid-cols-3 gap-2.5 mb-3">
                <div>
                  <label className="block eyebrow text-muted-foreground! mb-1" htmlFor={`edit-tm-${p.id}`}>TM limit</label>
                  <input
                    id={`edit-tm-${p.id}`}
                    type="number"
                    min="0"
                    value={editTmLimit}
                    onChange={(e) => setEditTmLimit(e.target.value)}
                    className="w-full border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary"
                  />
                </div>
                <div>
                  <label className="block eyebrow text-muted-foreground! mb-1" htmlFor={`edit-seats-${p.id}`}>Seat limit</label>
                  <input
                    id={`edit-seats-${p.id}`}
                    type="number"
                    min="1"
                    value={editSeatLimit}
                    onChange={(e) => setEditSeatLimit(e.target.value)}
                    className="w-full border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary"
                  />
                </div>
                <div>
                  <label className="block eyebrow text-muted-foreground! mb-1" htmlFor={`edit-price-${p.id}`}>Price (₹/year)</label>
                  <input
                    id={`edit-price-${p.id}`}
                    type="number"
                    min="0"
                    value={editPriceRupees}
                    onChange={(e) => setEditPriceRupees(e.target.value)}
                    className="w-full border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary"
                  />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-2.5 mb-3">
                <div>
                  <label className="block eyebrow text-muted-foreground! mb-1" htmlFor={`edit-search-${p.id}`}>TM Search limit (blank = unlimited)</label>
                  <input
                    id={`edit-search-${p.id}`}
                    type="number"
                    min="0"
                    value={editSearchLimit}
                    onChange={(e) => setEditSearchLimit(e.target.value)}
                    className="w-full border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary"
                  />
                </div>
                <div>
                  <label className="block eyebrow text-muted-foreground! mb-1" htmlFor={`edit-period-${p.id}`}>Search limit resets</label>
                  <select
                    id={`edit-period-${p.id}`}
                    value={editBillingPeriodMonths}
                    onChange={(e) => setEditBillingPeriodMonths(e.target.value)}
                    className="w-full border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary"
                  >
                    {BILLING_PERIODS.map((bp) => (
                      <option key={bp.value} value={bp.value}>{bp.label}</option>
                    ))}
                  </select>
                </div>
              </div>
              <div className="flex items-center gap-2">
                <button type="button" onClick={() => saveEdit(p)} disabled={saving} className="btn btn-accent inline-flex items-center gap-1.5 text-[12.5px] px-2.5 py-1.5">
                  <Check size={13} /> {saving ? 'Saving…' : 'Save'}
                </button>
                <button type="button" onClick={cancelEdit} disabled={saving} className="btn-ghost inline-flex items-center gap-1.5 text-[12.5px] px-2.5 py-1.5 rounded-md">
                  <X size={13} /> Cancel
                </button>
              </div>
            </div>
          ) : (
            <div key={p.id} className="flex items-center gap-3 px-4 py-3 border-b border-border last:border-b-0">
              <div className="min-w-0 flex-1">
                <div className="text-[13px] font-medium truncate flex items-center gap-1.5">
                  {p.name}
                  {!p.is_active && <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-status-bad-bg text-status-bad">inactive</span>}
                </div>
                <div className="text-[11.5px] text-muted-foreground truncate">
                  {formatPrice(p.price_cents)} · {p.tm_limit} TMs · {p.seat_limit} seat{p.seat_limit === 1 ? '' : 's'}
                  {' · '}{p.search_limit != null
                    ? `${p.search_limit} searches / ${BILLING_PERIODS.find((bp) => bp.value === p.billing_period_months)?.label.toLowerCase() || 'period'}`
                    : 'unlimited searches'}
                </div>
              </div>
              <div className="flex items-center gap-1.5 flex-shrink-0">
                <button
                  type="button"
                  onClick={() => startEdit(p)}
                  title="Edit limits and price"
                  className="btn-ghost p-1.5 rounded-md"
                >
                  <Pencil size={15} />
                </button>
                <button
                  type="button"
                  onClick={() => toggleActive(p)}
                  className="btn-ghost text-[12px] px-2 py-1 rounded-md"
                >
                  {p.is_active ? 'Deactivate' : 'Reactivate'}
                </button>
              </div>
            </div>
          )
        ))}
        {plans.length === 0 && (
          <div className="px-4 py-6 text-center text-[13px] text-muted-foreground">No plans yet — create one above.</div>
        )}
      </div>
    </div>
  );
}
