import { useEffect, useState } from 'react';
import Ring from '../../components/tm/Ring';
import { useNavigate, useParams, Link } from 'react-router-dom';
import { ArrowLeft, Receipt, Trash2 } from 'lucide-react';
import { tmApi } from '../../lib/tmApi';
import { kanbanStageClass, paymentStatusClass, formatDate, WORKFLOW_STEPS } from '../../lib/tmLead';
import Section from '../../components/tm/Section';
import { useToast } from '../../components/tm/Toast';
import { useConfirm } from '../../components/tm/ConfirmDialog';

const inputClass = 'border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary w-full';
const labelClass = 'eyebrow text-muted-foreground!';

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className={labelClass}>{label}</span>
      {children}
    </label>
  );
}

const MARK_STATUSES = [
  'Recently Applied', 'Marked for Exam', 'Formality Check Pass', 'Formality Check Fail',
  'Objection', 'Opposition', 'Reply Filed', 'Accepted & Advertised', 'Published',
  'Hearing', 'TM-M File', 'Pending for Examination', 'Examination Report File', 'Registered',
];

export default function LeadDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { showToast } = useToast();
  const confirmDialog = useConfirm();
  const [lead, setLead] = useState<any>(null);
  const [activities, setActivities] = useState<any[]>([]);
  const [consultants, setConsultants] = useState<any[]>([]);
  const [packages, setPackages] = useState<any[]>([]);
  const [saving, setSaving] = useState<string | null>(null);

  const load = () => {
    tmApi.getLead(id!).then((json) => { setLead(json.lead); setActivities(json.activities); }).catch((err) => {
      showToast('error', err.message || 'Lead not found');
      navigate('/tm/leads');
    });
  };

  useEffect(() => { load(); }, [id]);
  useEffect(() => {
    tmApi.listConsultants().then((json) => setConsultants(json.consultants)).catch(() => {});
    tmApi.listPackages().then((json) => setPackages(json.packages)).catch(() => {});
  }, []);

  const [info, setInfo] = useState<any>(null);
  useEffect(() => { if (lead) setInfo(lead); }, [lead?.id]);

  if (!lead || !info) return <div className="loading"><Ring state="loading" size={28} /><span>Loading lead…</span></div>;

  const runSave = async (key: string, fn: () => Promise<any>) => {
    setSaving(key);
    try {
      const updated = await fn();
      setLead(updated);
      tmApi.getLead(id!).then((json) => setActivities(json.activities));
      showToast('success', 'Saved.');
    } catch (err: any) {
      showToast('error', err.message || 'Save failed');
    } finally {
      setSaving(null);
    }
  };

  const deleteLead = async () => {
    const ok = await confirmDialog({
      title: `Delete lead "${lead.name}"?`,
      message: 'This cannot be undone.',
      confirmLabel: 'Delete',
      danger: true,
    });
    if (!ok) return;
    try {
      await tmApi.deleteLead(id!);
      navigate('/tm/leads');
    } catch (err: any) {
      showToast('error', err.message || 'Failed to delete lead');
    }
  };

  return (
    <div className="flex-1 min-h-0 flex flex-col overflow-hidden">
      <div className="px-5 pt-4 pb-2 flex-shrink-0 flex items-center justify-between">
        <button type="button" onClick={() => navigate('/tm/leads')} className="flex items-center gap-1.5 text-[13px] text-muted-foreground hover:text-foreground">
          <ArrowLeft size={14} /> Back to Leads
        </button>
        <div className="flex items-center gap-2">
          <Link to={`/tm/leads/${id}/invoice`} className="btn btn-accent"><Receipt size={13} /> Create / Edit Invoice</Link>
          <button type="button" onClick={deleteLead} className="btn btn-ghost text-status-bad"><Trash2 size={13} /> Delete</button>
        </div>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto px-5 pb-15">
        <div className="flex items-center gap-3 mb-4 flex-wrap">
          <h1 className="text-[19px] font-semibold m-0">{lead.name}</h1>
          <span className={`pill ${kanbanStageClass(lead.kanban_stage)}`}><span className="dot" />{lead.kanban_stage}</span>
          <span className={`pill ${paymentStatusClass(lead.payment_status)}`}><span className="dot" />{lead.payment_status}</span>
        </div>

        <div className="grid grid-cols-[1fr_320px] gap-4 items-start">
          <div>
            {/* Stage 1 — Lead Info */}
            <Section title="Lead Info" defaultOpen>
              <div className="grid grid-cols-2 gap-3.5">
                <Field label="Name"><input className={inputClass} value={info.name || ''} onChange={(e) => setInfo({ ...info, name: e.target.value })} /></Field>
                <Field label="Mobile"><input className={inputClass} value={info.mobile || ''} onChange={(e) => setInfo({ ...info, mobile: e.target.value })} /></Field>
                <Field label="Business Name"><input className={inputClass} value={info.business_name || ''} onChange={(e) => setInfo({ ...info, business_name: e.target.value })} /></Field>
                <Field label="Proposed Trademark"><input className={inputClass} value={info.proposed_trademark || ''} onChange={(e) => setInfo({ ...info, proposed_trademark: e.target.value })} /></Field>
                <Field label="Class"><input className={inputClass} value={info.class || ''} onChange={(e) => setInfo({ ...info, class: e.target.value })} /></Field>
                <Field label="Source"><input className={inputClass} value={info.source || ''} onChange={(e) => setInfo({ ...info, source: e.target.value })} /></Field>
                <Field label="Reference Name"><input className={inputClass} value={info.reference_name || ''} onChange={(e) => setInfo({ ...info, reference_name: e.target.value })} /></Field>
                <Field label="Entity Type"><input className={inputClass} value={info.entity_type || ''} onChange={(e) => setInfo({ ...info, entity_type: e.target.value })} /></Field>
                <Field label="State"><input className={inputClass} value={info.state || ''} onChange={(e) => setInfo({ ...info, state: e.target.value })} /></Field>
                <Field label="Assigned To"><input className={inputClass} value={info.assigned_to || ''} onChange={(e) => setInfo({ ...info, assigned_to: e.target.value })} /></Field>
              </div>
              <button
                type="button"
                className="btn btn-primary mt-3.5"
                disabled={saving === 'info'}
                onClick={() => runSave('info', () => tmApi.updateLead(id!, info))}
              >
                {saving === 'info' ? 'Saving…' : 'Save Lead Info'}
              </button>
            </Section>

            {/* Stage 2 — Qualification */}
            <Section title="Qualification">
              <div className="flex items-center gap-2 mb-3">
                {['Hot', 'Warm', 'Cold'].map((q) => (
                  <button
                    key={q}
                    type="button"
                    className={`btn ${info.qualification_status === q ? 'btn-primary' : ''}`}
                    onClick={() => setInfo({ ...info, qualification_status: q })}
                  >
                    {q}
                  </button>
                ))}
              </div>
              <Field label="Notes">
                <textarea className={inputClass} rows={2} value={info.qualification_notes || ''} onChange={(e) => setInfo({ ...info, qualification_notes: e.target.value })} />
              </Field>
              <button
                type="button"
                className="btn btn-primary mt-3.5"
                disabled={saving === 'qualify'}
                onClick={() => runSave('qualify', () => tmApi.qualify(id!, { qualification_status: info.qualification_status, qualification_notes: info.qualification_notes }))}
              >
                {saving === 'qualify' ? 'Saving…' : 'Save Qualification'}
              </button>
            </Section>

            {/* Stage 3 — Consultant Mapping */}
            <Section title="Consultant Mapping">
              <Field label="Consultant">
                <select
                  className={inputClass}
                  value={info.consultant_id || ''}
                  onChange={(e) => setInfo({ ...info, consultant_id: e.target.value || null })}
                >
                  <option value="">None (Direct)</option>
                  {consultants.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                </select>
              </Field>
              <button
                type="button"
                className="btn btn-primary mt-3.5"
                disabled={saving === 'consultant'}
                onClick={() => runSave('consultant', () => tmApi.mapConsultant(id!, { consultant_id: info.consultant_id }))}
              >
                {saving === 'consultant' ? 'Saving…' : 'Save Consultant'}
              </button>
            </Section>

            {/* Stage 4 — Payment */}
            <Section title="Payment">
              <div className="grid grid-cols-2 gap-3.5">
                <Field label="Package">
                  <select className={inputClass} value={info.package || ''} onChange={(e) => setInfo({ ...info, package: e.target.value })}>
                    <option value="">Custom</option>
                    {packages.map((p) => <option key={p.id} value={p.name}>{p.name}</option>)}
                  </select>
                </Field>
                <Field label="Pricing">
                  <input type="number" className={inputClass} value={info.pricing ?? ''} onChange={(e) => setInfo({ ...info, pricing: e.target.value })} />
                </Field>
              </div>
              <div className="flex items-center gap-2 mt-3">
                {['Payment Pending', 'Payment Done'].map((s) => (
                  <button
                    key={s}
                    type="button"
                    className={`btn ${info.payment_status === s ? 'btn-primary' : ''}`}
                    onClick={() => setInfo({ ...info, payment_status: s })}
                  >
                    {s}
                  </button>
                ))}
              </div>
              <button
                type="button"
                className="btn btn-primary mt-3.5"
                disabled={saving === 'payment'}
                onClick={() => runSave('payment', () => tmApi.updatePayment(id!, { pricing: info.pricing, package: info.package, payment_status: info.payment_status }))}
              >
                {saving === 'payment' ? 'Saving…' : 'Save Payment'}
              </button>
            </Section>

            {/* Stage 5 — Document Workflow */}
            <Section title="Document Workflow">
              <div className="flex flex-col gap-2">
                {WORKFLOW_STEPS.map((step) => {
                  const doneAt = lead[`${step.key}_at`];
                  return (
                    <div key={step.key} className="flex items-center justify-between py-2 border-b border-border last:border-b-0">
                      <label className="flex items-center gap-2.5 text-[13px] cursor-pointer">
                        <input
                          type="checkbox"
                          checked={!!doneAt}
                          onChange={(e) => runSave(`wf-${step.key}`, () => tmApi.updateWorkflow(id!, { step: step.key, done: e.target.checked }))}
                        />
                        {step.label}
                      </label>
                      <span className="text-[11.5px] font-mono text-muted-foreground">{doneAt ? formatDate(doneAt) : '—'}</span>
                    </div>
                  );
                })}
              </div>
            </Section>

            {/* Stage 6 — Filing */}
            <Section title="Filing">
              <div className="grid grid-cols-2 gap-3.5">
                <Field label="ARN Number"><input className={inputClass} value={info.arn_number || ''} onChange={(e) => setInfo({ ...info, arn_number: e.target.value })} /></Field>
                <Field label="Application No."><input className={inputClass} value={info.application_no || ''} onChange={(e) => setInfo({ ...info, application_no: e.target.value })} /></Field>
              </div>
              <div className="text-[11.5px] text-muted-foreground mt-2">Filed: {formatDate(lead.filed_at)}</div>
              <button
                type="button"
                className="btn btn-primary mt-3.5"
                disabled={saving === 'filing'}
                onClick={() => runSave('filing', () => tmApi.updateTracking(id!, { arn_number: info.arn_number, application_no: info.application_no }))}
              >
                {saving === 'filing' ? 'Saving…' : 'Save Filing'}
              </button>
            </Section>

            {/* Stage 7 — Tracking */}
            <Section title="Tracking">
              <div className="grid grid-cols-2 gap-3.5">
                <Field label="Mark Status">
                  <select className={inputClass} value={info.mark_status || ''} onChange={(e) => setInfo({ ...info, mark_status: e.target.value })}>
                    <option value="">—</option>
                    {MARK_STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
                  </select>
                </Field>
                <Field label="Action By Us"><input className={inputClass} value={info.action_by_us || ''} onChange={(e) => setInfo({ ...info, action_by_us: e.target.value })} /></Field>
                <Field label="Status Comment"><input className={inputClass} value={info.status_comment || ''} onChange={(e) => setInfo({ ...info, status_comment: e.target.value })} /></Field>
              </div>
              <button
                type="button"
                className="btn btn-primary mt-3.5"
                disabled={saving === 'tracking'}
                onClick={() => runSave('tracking', () => tmApi.updateTracking(id!, {
                  mark_status: info.mark_status, action_by_us: info.action_by_us, status_comment: info.status_comment, status: info.mark_status,
                }))}
              >
                {saving === 'tracking' ? 'Saving…' : 'Save Tracking'}
              </button>
            </Section>

            {/* Stage 8 — Objection */}
            <Section title="Objection / Opposition">
              <div className="grid grid-cols-2 gap-3.5">
                <Field label="Hearing Date">
                  <input type="date" className={inputClass} value={info.hearing_date ? String(info.hearing_date).slice(0, 10) : ''} onChange={(e) => setInfo({ ...info, hearing_date: e.target.value })} />
                </Field>
              </div>
              <div className="text-[11.5px] text-muted-foreground mt-2">Reply filed: {formatDate(lead.reply_filed_at)}</div>
              <div className="flex items-center gap-2 mt-3">
                <button
                  type="button"
                  className="btn"
                  disabled={saving === 'objection-reply'}
                  onClick={() => runSave('objection-reply', () => tmApi.updateObjection(id!, { reply_filed: true, status: 'Reply Filed' }))}
                >
                  Mark Reply Filed
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={saving === 'objection'}
                  onClick={() => runSave('objection', () => tmApi.updateObjection(id!, { hearing_date: info.hearing_date }))}
                >
                  {saving === 'objection' ? 'Saving…' : 'Save Hearing Date'}
                </button>
              </div>
            </Section>

            {/* Stage 9 — Publication */}
            <Section title="Publication">
              <div className="grid grid-cols-2 gap-3.5">
                <Field label="Journal Number"><input className={inputClass} value={info.journal_number || ''} onChange={(e) => setInfo({ ...info, journal_number: e.target.value })} /></Field>
              </div>
              <div className="text-[11.5px] text-muted-foreground mt-2">Published: {formatDate(lead.journal_published_at)}</div>
              <button
                type="button"
                className="btn btn-primary mt-3.5"
                disabled={saving === 'publication'}
                onClick={() => runSave('publication', () => tmApi.updatePublication(id!, { journal_number: info.journal_number, status: 'Accepted & Advertised' }))}
              >
                {saving === 'publication' ? 'Saving…' : 'Save Publication'}
              </button>
            </Section>

            {/* Stage 10 — Registration */}
            <Section title="Registration">
              <div className="grid grid-cols-2 gap-3.5">
                <Field label="Certificate Number"><input className={inputClass} value={info.certificate_number || ''} onChange={(e) => setInfo({ ...info, certificate_number: e.target.value })} /></Field>
              </div>
              <div className="text-[11.5px] text-muted-foreground mt-2">Registered: {formatDate(lead.registered_at)}</div>
              <button
                type="button"
                className="btn btn-primary mt-3.5"
                disabled={saving === 'registration'}
                onClick={() => runSave('registration', () => tmApi.updateRegistration(id!, { certificate_number: info.certificate_number }))}
              >
                {saving === 'registration' ? 'Saving…' : 'Save Registration'}
              </button>
            </Section>
          </div>

          <div>
            {/* Activity Log */}
            <div className="card sticky top-4">
              <div className="card-head"><h3>Activity Log</h3></div>
              {activities.length ? (
                <div className="flex flex-col gap-3 max-h-[600px] overflow-y-auto">
                  {activities.map((a) => (
                    <div key={a.id} className="flex flex-col gap-0.5 pb-2.5 border-b border-border last:border-b-0">
                      <div className="flex items-center gap-1.5">
                        <span className="pill s-info">{a.type}</span>
                      </div>
                      <div className="text-[12.5px] mt-1">{a.description}</div>
                      <div className="text-[10.5px] text-muted-foreground font-mono">{formatDate(a.created_at)}{a.performed_by ? ` · ${a.performed_by}` : ''}</div>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="font-mono text-muted-foreground text-[12px]">No activity yet.</p>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
