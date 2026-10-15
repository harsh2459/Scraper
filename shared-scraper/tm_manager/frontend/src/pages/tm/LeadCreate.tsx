import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { tmApi } from '../../lib/tmApi';

const SOURCES = ['Website', 'WhatsApp', 'Call', 'Referral', 'Ads'];
const ENTITY_TYPES = ['Individual', 'Proprietorship', 'Partnership', 'LLP', 'Company', 'Trust', 'Society'];

export default function LeadCreate() {
  const navigate = useNavigate();
  const [form, setForm] = useState({
    name: '', mobile: '', business_name: '', proposed_trademark: '', class: '',
    source: 'Website', reference_name: '', assigned_to: '', entity_type: '', state: '',
  });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  const set = (key: string) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm((f) => ({ ...f, [key]: e.target.value }));

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!form.name.trim()) { setError('Name is required.'); return; }
    setSubmitting(true);
    setError('');
    try {
      const lead = await tmApi.createLead(form);
      navigate(`/tm/leads/${lead.id}`);
    } catch (err: any) {
      setError(err.message || 'Failed to create lead.');
    } finally {
      setSubmitting(false);
    }
  };

  const inputClass = 'border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary w-full';
  const labelClass = 'eyebrow text-muted-foreground!';

  return (
    <div className="flex-1 min-h-0 overflow-y-auto px-7.5 pt-6 pb-15">
      <h1 className="text-[19px] font-semibold m-0 mb-5">Create Lead</h1>

      <form onSubmit={submit} className="max-w-[640px] card">
        {error && <div className="rounded-lg px-3 py-2 text-[12.5px] mb-3 bg-status-bad-bg text-status-bad">{error}</div>}
        <div className="grid grid-cols-2 gap-3.5">
          <label className="flex flex-col gap-1.5 col-span-2">
            <span className={labelClass}>Name *</span>
            <input className={inputClass} value={form.name} onChange={set('name')} required />
          </label>
          <label className="flex flex-col gap-1.5">
            <span className={labelClass}>Mobile</span>
            <input className={inputClass} value={form.mobile} onChange={set('mobile')} />
          </label>
          <label className="flex flex-col gap-1.5">
            <span className={labelClass}>Business Name</span>
            <input className={inputClass} value={form.business_name} onChange={set('business_name')} />
          </label>
          <label className="flex flex-col gap-1.5">
            <span className={labelClass}>Proposed Trademark</span>
            <input className={inputClass} value={form.proposed_trademark} onChange={set('proposed_trademark')} />
          </label>
          <label className="flex flex-col gap-1.5">
            <span className={labelClass}>Class</span>
            <input className={inputClass} value={form.class} onChange={set('class')} placeholder="e.g. 35" />
          </label>
          <label className="flex flex-col gap-1.5">
            <span className={labelClass}>Source</span>
            <select className={inputClass} value={form.source} onChange={set('source')}>
              {SOURCES.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </label>
          {form.source === 'Referral' && (
            <label className="flex flex-col gap-1.5">
              <span className={labelClass}>Reference Name</span>
              <input className={inputClass} value={form.reference_name} onChange={set('reference_name')} />
            </label>
          )}
          <label className="flex flex-col gap-1.5">
            <span className={labelClass}>Entity Type</span>
            <select className={inputClass} value={form.entity_type} onChange={set('entity_type')}>
              <option value="">Select…</option>
              {ENTITY_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </label>
          <label className="flex flex-col gap-1.5">
            <span className={labelClass}>State</span>
            <input className={inputClass} value={form.state} onChange={set('state')} />
          </label>
          <label className="flex flex-col gap-1.5">
            <span className={labelClass}>Assigned To</span>
            <input className={inputClass} value={form.assigned_to} onChange={set('assigned_to')} />
          </label>
        </div>

        <div className="flex items-center gap-2 mt-5">
          <button type="submit" className="btn btn-accent" disabled={submitting}>
            {submitting ? 'Creating…' : 'Create Lead'}
          </button>
          <button type="button" className="btn btn-ghost" onClick={() => navigate('/tm/leads')}>Cancel</button>
        </div>
      </form>
    </div>
  );
}
