import { useState } from 'react';
import { API_BASE } from '../lib/api';

export const EVENT_LABELS: Record<string, string> = {
  examination_report_reply: 'Examination Report Reply', reply_us_132: 'Reply Exm Rpt u/s 132',
  hearing_show_cause: 'Hearing (Show Cause)', hearing_opposition: 'Hearing (Opposition)',
  counter_statement: 'Counter Statement', application_accepted: 'Application accepted',
  evidence_rule_45: 'Evidence — Rule 45', evidence_rule_46: 'Evidence — Rule 46', evidence_rule_47: 'Evidence — Rule 47',
};

export default function ComplianceReview({ event, onSaved }: { event: any; onSaved: () => void }) {
  const [open, setOpen] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  return <div className="rounded-lg border border-border bg-panel p-3 text-[13px]">
    <div className="flex items-center justify-between gap-3 flex-wrap">
      <div><strong>{EVENT_LABELS[event.category] || event.category}</strong> · #{event.app_no}
        {event.opposition_no && <span> · Opp. {event.opposition_no}</span>}
        <div className="text-muted-foreground mt-1">{event.due_date || 'No date yet'} · {event.needs_review ? 'Needs review' : event.due_basis === 'confirmed' ? 'Confirmed by you' : 'Stated in notice'} · {String(event.status || 'pending').replaceAll('_', ' ')}</div>
      </div>
      <div className="flex gap-2">{event.document_url && <a className="btn" target="_blank" rel="noopener noreferrer" href={API_BASE + event.document_url}>Source notice</a>}
        {event.source === 'event' && <button className="btn" onClick={() => setOpen(!open)}>{open ? 'Close review' : 'Review / update'}</button>}</div>
    </div>
    {open && <form className="mt-3 flex flex-col gap-3" onSubmit={async e => {
      e.preventDefault(); setBusy(true); setError('');
      const form = new FormData(e.currentTarget);
      const body: Record<string, unknown> = { status: form.get('status'), receipt_date: form.get('receipt_date') || null, notes: form.get('notes') || '' };
      if (form.get('confirm') === 'on') {
        if (!form.get('due_date')) {setError('Enter the deadline you want to confirm.'); setBusy(false); return;}
        body.confirmed_due_date = form.get('due_date');
      }
      if (form.get('clear') === 'on') body.confirmed_due_date = null;
      try {
        const response = await fetch(`${API_BASE}/api/compliance-events/${event.source_id}`, {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
        const json = await response.json(); if (!response.ok) throw new Error(json.error || 'Save failed');
        setOpen(false); onSaved();
      } catch (err) {setError((err as Error).message);} finally {setBusy(false);}
    }}>
      <p className="text-muted-foreground whitespace-pre-wrap">{event.facts?.excerpt || event.subject || 'No document text extracted.'}</p>
      {event.due_basis === 'confirmation_stale' && <p className="text-status-warn">The source changed after your confirmation. Check the latest notice and confirm the date again.</p>}
      {event.period_value && <p>Notice period: {event.period_value} {event.period_unit}, from {event.anchor?.replaceAll('_', ' ')}. Record the actual receipt date to calculate a suggestion.</p>}
      <div className="flex gap-3 flex-wrap">
        <label>Receipt date<input className="input block" name="receipt_date" type="date" defaultValue={String(event.receipt_date || '').slice(0, 10)} /></label>
        <label>Deadline<input className="input block" name="due_date" type="date" defaultValue={event.due_date || ''} /></label>
        <label>Status<select className="input block" name="status" defaultValue={event.status || 'pending'}>{['pending', 'in_progress', 'completed', 'closed'].map(s => <option key={s} value={s}>{s.replaceAll('_', ' ')}</option>)}</select></label>
      </div>
      <label><input name="confirm" type="checkbox" /> I checked the source and confirm the deadline above.</label>
      {event.confirmed_at && <label><input name="clear" type="checkbox" /> Clear my previous deadline confirmation.</label>}
      <label>Private notes<textarea className="input block w-full" name="notes" maxLength={4000} defaultValue={event.notes || ''} /></label>
      {error && <p role="alert" className="text-status-bad">{error}</p>}
      <button className="btn btn-accent self-start" disabled={busy}>{busy ? 'Saving…' : 'Save review'}</button>
    </form>}
  </div>;
}
