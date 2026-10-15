import { useEffect, useState, useCallback } from 'react';
import { Link } from 'react-router-dom';
import Ring from './tm/Ring';
import DocumentViewerModal from './DocumentViewerModal';
import { X, Printer, RefreshCw, Menu, ChevronRight, ChevronDown, CalendarClock, ArrowLeft, Clock, CheckCircle2, Circle, XCircle } from 'lucide-react';
import { statusClass } from '../lib/status';
import { API_BASE } from '../lib/api';
import ComplianceReview, { EVENT_LABELS } from './ComplianceReview';

const PAGE_TABS = ['Overview', 'Timeline', 'Documents', 'Notes', 'Tasks'] as const;
type PageTab = (typeof PAGE_TABS)[number];

// Same labels as Tasks.tsx's compliance calendar — kept in sync so a
// category reads the same wherever it shows up.
const COMPLIANCE_LABELS: Record<string, string> = {
  ...EVENT_LABELS,
  examination_report_reply: 'Examination Report Reply',
  reply_us_132: 'Reply Exm Rpt u/s 132',
  hearing_show_cause: 'Hearing (Show Cause)',
  counter_statement: 'Counter Statement',
};

const STATUS_LABELS: Record<string, string> = {
  pending: 'Pending',
  in_progress: 'In Progress',
  completed: 'Completed',
  closed: 'Closed',
};

const STATUS_ICONS: Record<string, typeof Clock> = {
  pending: Clock,
  in_progress: Circle,
  completed: CheckCircle2,
  closed: XCircle,
};

const STATUS_TEXT_CLASS: Record<string, string> = {
  pending: 'text-status-info',
  in_progress: 'text-status-warn',
  completed: 'text-status-good',
  closed: 'text-muted-foreground',
};

function formatTaskDate(dateStr: string | null | undefined): { text: string; isToday: boolean } {
  if (!dateStr) return { text: '—', isToday: false };
  // Accepts either DD/MM/YYYY (reminders/correspondence) or YYYY-MM-DD
  // (custom_tasks/compliance due_date) — normalize both to DD/MM/YYYY for
  // display, and compare against today to flag "(Today)".
  let display = dateStr;
  let iso = dateStr;
  if (dateStr.includes('/')) {
    const [d, m, y] = dateStr.split('/');
    iso = `${y}-${m}-${d}`;
  } else {
    const [y, m, d] = dateStr.split('-');
    display = `${d}/${m}/${y}`;
  }
  const isToday = iso === new Date().toISOString().slice(0, 10);
  return { text: display, isToday };
}

function TaskStatusMenu({ status, onChange }: { status: string; onChange: (next: string) => void }) {
  const [open, setOpen] = useState(false);
  const Icon = STATUS_ICONS[status] || Clock;
  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={`inline-flex items-center gap-1 text-[12.5px] font-medium ${STATUS_TEXT_CLASS[status] || 'text-muted-foreground'}`}
      >
        <Icon size={13} />
        {STATUS_LABELS[status] || status}
        <ChevronDown size={12} />
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
          <div className="absolute right-0 top-[calc(100%+4px)] z-20 w-36 bg-panel border border-border rounded-lg shadow-lg py-1">
            {Object.keys(STATUS_LABELS).map((s) => {
              const OptIcon = STATUS_ICONS[s];
              return (
                <button
                  key={s}
                  type="button"
                  className={`w-full flex items-center gap-1.5 px-3 py-1.5 text-[12.5px] text-left hover:bg-muted ${STATUS_TEXT_CLASS[s]}`}
                  onClick={() => { onChange(s); setOpen(false); }}
                >
                  <OptIcon size={13} /> {STATUS_LABELS[s]}
                </button>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}

export default function DetailPane({
  tmId,
  onClose,
  variant = 'overlay',
  breadcrumbLabel = 'Portfolio',
  breadcrumbTo = '/trademarks',
}: {
  tmId: number;
  onClose: () => void;
  variant?: 'page' | 'overlay';
  breadcrumbLabel?: string;
  breadcrumbTo?: string;
}) {
  const [data, setData] = useState<any>(null);
  const [imgError, setImgError] = useState(false);
  const [showNotes, setShowNotes] = useState(false);
  const [savedNotes, setSavedNotes] = useState('');
  const [draftNotes, setDraftNotes] = useState('');
  const [savingNotes, setSavingNotes] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshStatus, setRefreshStatus] = useState<'idle' | 'queued' | 'fetching' | 'done' | 'error'>('idle');
  const [refreshMsg, setRefreshMsg] = useState('');
  const [activeTab, setActiveTab] = useState<PageTab>('Overview');
  const [tasks, setTasks] = useState<any[] | null>(null);
  const [viewerDoc, setViewerDoc] = useState<{ url: string; title: string } | null>(null);

  const loadData = useCallback(() => {
    fetch(`${API_BASE}/api/trademark/${tmId}`)
      .then((res) => res.json())
      .then((json) => {
        setData(json);
        setSavedNotes(json.tm?.notes || '');
        setDraftNotes(json.tm?.notes || '');
      })
      .catch((err) => console.error(err));
  }, [tmId]);

  useEffect(() => {
    setImgError(false);
    setData(null);
    setShowNotes(false);
    setRefreshing(false);
    setRefreshStatus('idle');
    setRefreshMsg('');
    setActiveTab('Overview');
    setTasks(null);
    loadData();
  }, [tmId, loadData]);

  // Poll the trademark's own status until it leaves 'fetching'/'pending'
  useEffect(() => {
    if (!refreshing) return;
    const interval = setInterval(() => {
      fetch(`${API_BASE}/api/trademark/${tmId}`)
        .then((res) => res.json())
        .then((json) => {
          const st = json.tm?.status;
          if (st === 'fetched') {
            setData(json);
            setSavedNotes(json.tm?.notes || '');
            setDraftNotes(json.tm?.notes || '');
            setRefreshing(false);
            setRefreshStatus('done');
            setRefreshMsg('Data updated.');
            setTimeout(() => setRefreshStatus('idle'), 3000);
          } else if (st === 'error') {
            setRefreshing(false);
            setRefreshStatus('error');
            setRefreshMsg(json.tm?.error_message || 'Fetch failed.');
            setTimeout(() => setRefreshStatus('idle'), 5000);
          } else {
            // still pending or fetching
            setRefreshStatus('fetching');
          }
        })
        .catch(() => { });
    }, 4000);
    return () => clearInterval(interval);
  }, [refreshing, tmId]);

  // Tasks tab: lazy-load the portfolio-wide reminders + compliance +
  // custom-task feed, filtered to this trademark. Reminders alone
  // (ipindia_reminders) miss anything that only ever showed up as
  // correspondence (e.g. a "Reply to Examination Report" notice) — those
  // are what list_compliance_notices buckets on the backend and what the
  // Tasks & Compliances calendar actually renders, so this tab has to pull
  // from the same three sources that page does or a trademark can show
  // real upcoming items there while this tab says "No reminders on file."
  useEffect(() => {
    if (variant !== 'page' || activeTab !== 'Tasks' || tasks !== null) return;
    fetch(`${API_BASE}/api/tasks-compliances?app_no=${encodeURIComponent(data?.tm?.app_no || '')}`)
      .then((res) => res.json())
      .then((json) => {
        // kind distinguishes RENDERING (plain reminder row vs. a
        // compliance-bucketed one vs. a custom task) from `source`/
        // `source_id`, which is the real registry-row identity
        // (list_reminders/list_correspondence's own join key — see
        // db.set_compliance_status) needed to PATCH a status. A compliance
        // row's `source` is already 'reminder' or 'correspondence' as
        // attached by the backend; only `kind` is set here, so that
        // identity survives untouched.
        const reminderRows = (json.reminders || [])
          .filter((r: any) => r.tm_id === tmId)
          .map((r: any) => ({ ...r, kind: 'reminder' as const }));
        const complianceRows = Object.entries(json.compliance || {}).flatMap(([category, items]: [string, any]) =>
          (items || [])
            .filter((r: any) => r.tm_id === tmId)
            .map((r: any) => ({ ...r, kind: 'compliance' as const, category }))
        );
        const customRows = (json.custom_tasks || [])
          .filter((t: any) => t.my_trademark_id === tmId)
          .map((t: any) => ({ ...t, kind: 'custom' as const }));
        const rows = [...reminderRows, ...complianceRows, ...customRows];
        rows.sort((a, b) => {
          const da = a.despatch_date || a.corres_date || a.due_date || '';
          const db_ = b.despatch_date || b.corres_date || b.due_date || '';
          return db_.localeCompare(da);
        });
        setTasks(rows);
      })
      .catch(() => setTasks([]));
  }, [variant, activeTab, tasks, tmId]);

  const changeTaskStatus = async (row: any, next: string) => {
    setTasks((prev) =>
      (prev || []).map((r: any) =>
        r === row ? { ...r, status: next } : r
      )
    );
    try {
      if (row.kind === 'custom') {
        await fetch(`${API_BASE}/api/tasks/${row.id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status: next }),
        });
      } else {
        // reminder or compliance row — both are registry notices tracked
        // via compliance_status, keyed by the real source/source_id the
        // backend attached (see db.list_reminders/list_correspondence).
        await fetch(`${API_BASE}/api/compliance-status`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ source: row.source, source_id: row.source_id, status: next }),
        });
      }
    } catch {
      setTasks(null); // reload from scratch on failure rather than risk showing a stale/wrong status
    }
  };

  const handleLiveRefresh = async () => {
    if (refreshing) return;
    setRefreshing(true);
    setRefreshStatus('queued');
    setRefreshMsg('Queuing re-fetch…');
    try {
      const res = await fetch(`${API_BASE}/api/trademark/${tmId}/live-refresh`, { method: 'POST' });
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      setRefreshMsg('Waiting for scraper…');
    } catch (err) {
      setRefreshing(false);
      setRefreshStatus('error');
      setRefreshMsg('Could not queue re-fetch. Is the backend running?');
      setTimeout(() => setRefreshStatus('idle'), 5000);
    }
  };

  const openNotes = () => {
    setDraftNotes(savedNotes);
    setShowNotes(true);
  };

  const cancelNotes = () => {
    setDraftNotes(savedNotes);
    setShowNotes(false);
  };

  const saveNotes = async () => {
    setSavingNotes(true);
    try {
      await fetch(`${API_BASE}/api/trademark/${tmId}/notes`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ notes: draftNotes }),
      });
      setSavedNotes(draftNotes);
      setShowNotes(false);
    } catch (err) {
      console.error(err);
    } finally {
      setSavingNotes(false);
    }
  };

  if (!data) {
    return <div className="loading"><Ring state="loading" size={28} /><span>Loading details…</span></div>;
  }

  const { tm, data: tmData, documents, history, status_prev_current: statusPC } = data;
  const tmStatus = tmData?.status || tm.status;
  const tmName = tmData?.trade_mark || tm.label || tm.app_no;

  // Refresh button label + style
  const refreshLabel = refreshStatus === 'queued'
    ? 'Queuing…'
    : refreshStatus === 'fetching'
      ? 'Fetching…'
      : refreshStatus === 'done'
        ? 'Updated ✓'
        : refreshStatus === 'error'
          ? 'Failed'
          : 'Live refresh';

  const refreshBtnClass = refreshStatus === 'done'
    ? 'btn border-status-good text-status-good'
    : refreshStatus === 'error'
      ? 'btn border-status-bad text-status-bad'
      : 'btn';

  const renewalDate = parseDate(tmData?.valid_upto);
  const avatar = tmData?.has_logo && !imgError ? (
    <div className="w-14 h-14 flex-shrink-0 flex items-center justify-center">
      <img
        src={`${API_BASE}/trademark/${tm.id}/logo`}
        alt=""
        className="max-w-full max-h-full w-auto h-auto object-contain block"
        onError={() => setImgError(true)}
      />
    </div>
  ) : (
    <div className="w-14 h-14 rounded-lg bg-primary text-primary-foreground flex items-center justify-center text-[20px] font-display font-semibold flex-shrink-0">
      {tmName?.substring(0, 1).toUpperCase()}
    </div>
  );

  const toolbar = (
    <div className="flex gap-2 items-center">
      <button className="btn" onClick={() => window.print()}><Printer size={14} /> Print</button>
      <button
        className={refreshBtnClass}
        onClick={handleLiveRefresh}
        disabled={refreshing}
        title="Re-scrape this trademark from the IP India portal right now"
      >
        <RefreshCw size={14} className={refreshing ? 'animate-spin' : ''} />
        {refreshLabel}
      </button>
      {variant === 'overlay' && (
        <button className="w-7 h-7 rounded-md border border-border-strong flex items-center justify-center flex-shrink-0" onClick={onClose}><X size={16} /></button>
      )}
    </div>
  );

  const refreshBanner = refreshStatus !== 'idle' && refreshMsg && (
    <div className={`px-5 py-2 text-[12.5px] flex items-center gap-2 flex-shrink-0 ${refreshStatus === 'done' ? 'bg-status-good-bg text-status-good' :
      refreshStatus === 'error' ? 'bg-status-bad-bg text-status-bad' :
        'bg-status-info-bg text-status-info'
      }`}>
      {refreshing && <RefreshCw size={12} className="animate-spin flex-shrink-0" />}
      {refreshMsg}
      {refreshStatus === 'fetching' && (
        <span className="ml-1 text-[11px] opacity-70">{data?.fleet_control_enabled ? 'Waiting for a worker to deliver the update.' : '(this can take 60–90s while the scraper logs in)'}</span>
      )}
    </div>
  );

  const notesPanel = (
    <div className="border border-border rounded-md overflow-hidden">
      <div
        className={`flex items-center justify-between px-3 py-2.5 bg-muted text-[13px] text-muted-foreground ${showNotes ? '' : 'cursor-pointer'}`}
        onClick={() => { if (!showNotes) openNotes(); }}
      >
        <div className="flex items-center gap-1.5"><Menu size={14} /> Notes</div>
        {showNotes ? (
          <div className="flex items-center gap-3">
            <button type="button" className="text-foreground font-normal hover:text-primary" onClick={(e) => { e.stopPropagation(); cancelNotes(); }}>Cancel</button>
            <button
              type="button"
              className="text-primary font-medium disabled:opacity-50"
              disabled={savingNotes}
              onClick={(e) => { e.stopPropagation(); saveNotes(); }}
            >
              {savingNotes ? 'Saving…' : 'Save'}
            </button>
          </div>
        ) : (
          <span>{savedNotes ? '' : '+'}</span>
        )}
      </div>

      {showNotes && (
        <textarea
          value={draftNotes}
          onChange={(e) => setDraftNotes(e.target.value)}
          autoFocus
          className="w-full min-h-[200px] border-none px-3 py-2.5 font-inherit text-[13px] text-foreground resize-y outline-none"
          placeholder="Write notes here…"
        />
      )}
      {!showNotes && savedNotes && (
        <div className="px-3 py-2.5 text-[13px] text-foreground whitespace-pre-wrap cursor-pointer min-h-[80px]" onClick={openNotes}>{savedNotes}</div>
      )}
      {!showNotes && !savedNotes && (
        <div className="px-3 py-4 text-[13px] text-muted-foreground cursor-pointer" onClick={openNotes}>No notes yet — click to add.</div>
      )}
    </div>
  );

  const documentsPanel = (
    <div>
      {tmData?.proceedings?.length > 0 && <section className="mb-4">
        <h4 className="text-[13px] font-semibold mb-2">Opposition proceedings</h4>
        {tmData.proceedings.map((p: any) => <div key={`${p.portal_type}-${p.opposition_no}`} className="rounded-lg border border-border p-3 mb-2 text-[13px]">
          <strong>Opp. {p.opposition_no}</strong> · {p.facts?.record?.proceeding_status || 'Opposition'}
          <p className="mt-1 text-muted-foreground">{p.facts?.record?.opponent_name || p.facts?.opponent_name_address}</p>
          <p className="mt-1">Filed: {p.facts?.record?.filed_date || p.facts?.filed_date || 'Date not found'}</p>
          <p className="mt-1">Decision: {p.facts?.record?.decision || 'No decision recorded'}</p>
          {tm.kind === 'opposition_out' && <button className="btn mt-2" onClick={async () => {
            const response = await fetch(`${API_BASE}/api/trademark/${tm.id}/proceeding-membership`, {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({proceeding_key: p.facts.proceeding_key, tracked: !p.tracked_by_you})});
            if (response.ok) {loadData(); setTasks(null);}
          }}>{p.tracked_by_you ? 'Remove from my opposition tasks' : 'This is my opposition — include its tasks'}</button>}
        </div>)}
      </section>}
      {tmData?.document_coverage?.length > 0 && <section className="mb-4">
        <h4 className="text-[13px] font-semibold mb-2">Documents from the registry</h4>
        {tmData.document_coverage.map((doc: any) => <div key={doc.source_key} className="py-1 text-[13px]">
          <span>{doc.download_state !== 'failed' ? <a className="app-no-link" target="_blank" rel="noopener noreferrer" href={`${API_BASE}/api/trademark/${tm.id}/source-document?source_key=${encodeURIComponent(doc.source_key)}`}>{doc.metadata?.description || doc.metadata?.subject || doc.kind.replaceAll('_', ' ')}</a> : doc.metadata?.description || doc.metadata?.subject || doc.kind.replaceAll('_', ' ')}</span>
        </div>)}
      </section>}
      <h4 className="text-[13px] font-semibold m-0 mb-2">Uploaded documents</h4>
      {documents && documents.length > 0 ? (
        documents.map((doc: string) => (
          <div key={doc} className="flex justify-between items-center py-2 border-b border-border last:border-b-0 text-[13px]">
            <button
              type="button"
              onClick={() => setViewerDoc({ url: `${API_BASE}/trademark/${tm.id}/document/${encodeURIComponent(doc)}`, title: doc })}
              className="text-primary font-medium hover:underline text-left"
            >
              {doc}
            </button>
            <span className="text-muted-foreground font-mono text-[12px]">{tmData?.application_date || '—'}</span>
          </div>
        ))
      ) : (
        <p className="font-mono text-muted-foreground text-[12px]">No documents uploaded.</p>
      )}

      {tmData?.reminders && tmData.reminders.length > 0 && (
        <div className="pt-4 mt-4 border-t border-border">
          <h4 className="text-[13px] font-semibold m-0 mb-2">Reminders</h4>
          {tmData.reminders.map((r: any, i: number) => (
            <div key={r.corres_no || i} className="flex justify-between items-center py-2 border-b border-border last:border-b-0 text-[13px]">
              {r.document_path ? (
                <button
                  type="button"
                  onClick={() => setViewerDoc({
                    url: `${API_BASE}/trademark/${tmId}/notice/reminders/${encodeURIComponent(r.document_path)}`,
                    title: r.subject || 'Reminder',
                  })}
                  className="text-primary hover:underline text-left"
                >
                  {r.subject || '—'}
                </button>
              ) : (
                <span>{r.subject || '—'}</span>
              )}
              <span className="text-muted-foreground font-mono text-[12px]">{r.corres_date || '—'}</span>
            </div>
          ))}
        </div>
      )}

      {tmData?.correspondence && tmData.correspondence.length > 0 && (
        <div className="pt-4 mt-4 border-t border-border">
          <h4 className="text-[13px] font-semibold m-0 mb-2">Correspondence &amp; Notices</h4>
          {tmData.correspondence.map((c: any, i: number) => (
            <div key={c.corres_no || i} className="flex justify-between items-center py-2 border-b border-border last:border-b-0 text-[13px]">
              {c.document_path ? (
                <button
                  type="button"
                  onClick={() => setViewerDoc({
                    url: `${API_BASE}/trademark/${tmId}/notice/correspondence/${encodeURIComponent(c.document_path)}`,
                    title: c.subject || 'Correspondence',
                  })}
                  className="text-primary hover:underline text-left"
                >
                  {c.subject || '—'}
                </button>
              ) : (
                <span>{c.subject || '—'}</span>
              )}
              <span className="text-muted-foreground font-mono text-[12px]">{c.corres_date || '—'}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );

  const overviewPanel = (
    <div>
      <div className="grid grid-cols-2 gap-y-3 pb-3.5">
        <Field label="Application No." value={tm.app_no} mono col="left" />
        <Field label="Class" value={tmData?.class_number || tm.class_number || '—'} col="right" />
        <Field label="Application Date" value={tmData?.application_date || tm.application_date || '—'} col="left" />
        <Field label="User Detail" value={tmData?.use_statement || tmData?.application_filed_as || '—'} col="right" />
        <Field label="State" value={tmData?.state || '—'} col="left" />
        <Field label="Office" value={tmData?.office || tmData?.registry_office || '—'} col="right" />
        <Field label="Country" value={tmData?.country || '—'} col="left" />
        <Field label="Type" value={tmData?.mark_category || tmData?.tm_type || '—'} col="right" />
        <Field label="Category" value={tmData?.category || 'Trade Mark'} col="left" />
        <Field label="Sub Status" value={tmData?.sub_status || '—'} col="right" />
        <Field label="Filing Mode" value={tmData?.application_filed_as || '—'} col="left" />
        <Field label="Publication Details" value={tmData?.publication_date || '—'} col="right" />
        <Field label="Valid Upto / Renewed Upto" value={tmData?.valid_upto || '—'} col="left" />
        <Field label="Certificate Detail" value={tmData?.certificate_detail || '—'} col="right" />
      </div>

      {tmData?.applicant_name && (
        <div className="py-3.5 border-t border-border">
          <h4 className="text-[13px] font-semibold m-0 mb-2">Owner</h4>
          <div className="text-[13.5px]">{tmData.applicant_name}</div>
          {tmData.applicant_address && <div className="text-[13.5px] mt-2 text-muted-foreground whitespace-pre-line">{tmData.applicant_address}</div>}
          {tmData.applicant_email && <div className="text-[13px] mt-1 text-muted-foreground">{tmData.applicant_email}</div>}
          {tmData.applicant_nature && <div className="text-[13px] mt-1 text-muted-foreground">Nature: {tmData.applicant_nature}</div>}
        </div>
      )}

      {tmData?.agent_name && (
        <div className="py-3.5 border-t border-border">
          <h4 className="text-[13px] font-semibold m-0 mb-2">Attorney</h4>
          <div className="text-[13.5px]">{tmData.agent_name}</div>
          {tmData.agent_address && <div className="text-[13.5px] mt-2 text-muted-foreground whitespace-pre-line">{tmData.agent_address}</div>}
        </div>
      )}

      {tmData?.class_description && (
        <div className="py-3.5 border-t border-border">
          <h4 className="text-[13px] font-semibold m-0 mb-2">Class detail</h4>
          <div className="text-[13px] leading-relaxed">
            <b className="bg-status-good-bg text-status-good px-1.5 py-px rounded font-semibold">Class {tmData.class_number || '—'}</b> &mdash; {tmData.class_description}
          </div>
        </div>
      )}

      {/* From the e-main page (RegisteredTM/Details) — not returned by the main status-page scrape at all */}
      {tmData?.goods_and_services && (
        <div className="py-3.5 border-t border-border">
          <h4 className="text-[13px] font-semibold m-0 mb-2">Goods &amp; Service Details</h4>
          <div className="text-[13px] leading-relaxed whitespace-pre-line">{tmData.goods_and_services}</div>
        </div>
      )}

      {/* PR Details — from the portal's own "PR Details" panel (RegisteredTM/GetPRDetails): proprietor/
          registration change history, when there is any. Plain prose (or "No Record Found"), not a
          label/value table like the e-main page above — shown as extracted plain text rather than the raw
          HTML dump, so it reads consistently with the rest of this page. Suppressed when it's just the
          "no history" message, since that's the common case for a new application and isn't worth a
          section of its own. */}
      {tmData?.pr_details_text && !/no record found/i.test(tmData.pr_details_text) && (
        <div className="py-3.5 border-t border-border">
          <h4 className="text-[13px] font-semibold m-0 mb-2">PR Details</h4>
          <div className="text-[13px] leading-relaxed whitespace-pre-line">{tmData.pr_details_text}</div>
        </div>
      )}
    </div>
  );

  const timelinePanel = (
    <div>
      {statusPC?.changed && (
        <div className="flex items-center gap-2 py-2.5 border-b border-border text-[13px]">
          <span className={`pill ${statusClass(statusPC.previous)}`}>{statusPC.previous || '—'}</span>
          <ChevronRight size={14} className="text-muted-foreground" />
          <span className={`pill ${statusClass(statusPC.current)}`}>{statusPC.current || '—'}</span>
          <span className="ml-auto text-muted-foreground font-mono text-[12px]">{statusPC.changed_at || '—'}</span>
        </div>
      )}
      <h4 className="text-[13px] font-semibold m-0 mb-2 mt-3.5">Fetch history</h4>
      {history && history.length > 0 ? (
        history.map((h: any) => (
          <div key={h.id} className="flex items-center gap-2 py-2 border-b border-border last:border-b-0 text-[13px]">
            <CalendarClock size={14} className="text-muted-foreground flex-shrink-0" />
            <span className="text-muted-foreground font-mono text-[12px]">{h.scraped_at}</span>
          </div>
        ))
      ) : (
        <p className="font-mono text-muted-foreground text-[12px]">No fetch history yet.</p>
      )}
    </div>
  );

  const tasksPanel = (
    <div className="flex flex-col gap-2">
      {tasks === null ? (
        <div className="text-[13px] text-muted-foreground">Loading…</div>
      ) : tasks.length === 0 ? (
        <p className="font-mono text-muted-foreground text-[12px]">No reminders, compliance items or tasks on file for this trademark.</p>
      ) : (
        tasks.map((r: any, i: number) => {
          if (r.source === 'event') return <ComplianceReview key={`event-${r.source_id}`} event={r} onSaved={() => setTasks(null)} />;
          const status = r.status || 'pending';
          const isDone = status === 'completed' || status === 'closed';
          const { text: dateText, isToday } = formatTaskDate(r.despatch_date || r.corres_date || r.due_date);
          // Compliance rows show their short category label ("Examination
          // Report Reply") as the headline, not the raw noisy registry
          // subject text — reminders/custom tasks have nothing shorter, so
          // those fall back to their own subject/title as before.
          const headline = r.kind === 'compliance' ? (COMPLIANCE_LABELS[r.category] || r.category) : (r.subject || r.title || '—');
          return (
            <div key={r.id ? `custom-${r.id}` : `${r.source}-${r.source_id}` || i} className="flex items-center gap-3 rounded-lg bg-muted px-3.5 py-2.5">
              <span className={`flex-1 min-w-0 truncate text-[13px] font-medium px-1.5 py-0.5 rounded bg-status-warn-bg text-status-warn ${isDone ? 'line-through opacity-60' : ''}`}>
                {headline}
              </span>
              <span className="text-[12.5px] text-muted-foreground font-mono whitespace-nowrap">
                {dateText}{isToday && <span className="text-status-warn font-medium ml-1">(Today)</span>}
              </span>
              <TaskStatusMenu status={status} onChange={(next) => changeTaskStatus(r, next)} />
            </div>
          );
        })
      )}
    </div>
  );

  if (variant === 'overlay') {
    return (
      <div className="flex flex-col h-full min-h-0">
        <div className="flex items-center justify-between gap-2.5 px-5 py-3 border-b border-border flex-shrink-0 bg-panel flex-wrap">
          <div className="flex items-center gap-3 min-w-0">
            <span className="app-no-link">#{tm.app_no}</span>
          </div>
          {toolbar}
        </div>
        {refreshBanner}
        <div className="flex-1 min-h-0 overflow-y-auto flex">
          <div className="flex-1 min-w-0 px-5 py-4.5 max-w-[760px]">
            <div className="flex justify-between items-start gap-4 mb-4">
              <div>
                <h2 className="text-[18px] font-semibold m-0">{tmName}</h2>
                <div className="mt-2">
                  <span className={`pill ${statusClass(tmStatus)}`}><span className="dot" />{tmStatus}</span>
                </div>
              </div>
              <div className="max-w-[220px] max-h-[120px] flex-shrink-0">{avatar}</div>
            </div>
            <div className="border-t border-border pt-3.5">{overviewPanel}</div>
            <div className="pt-3.5 border-t border-border">{documentsPanel}</div>
          </div>
          <div className="w-[320px] flex-shrink-0 border-l border-border px-5 py-4.5">
            {notesPanel}
          </div>
        </div>
        {viewerDoc && (
          <DocumentViewerModal url={viewerDoc.url} title={viewerDoc.title} onClose={() => setViewerDoc(null)} />
        )}
      </div>
    );
  }

  // variant === 'page'
  const summaryBits = [
    tmData?.application_date && `Filed ${tmData.application_date}`,
    (tmData?.class_number || tm.class_number) && `Class ${tmData?.class_number || tm.class_number}`,
    (tmData?.mark_category || tmData?.tm_type) && (tmData?.mark_category || tmData?.tm_type),
    (tmData?.office || tmData?.registry_office) && (tmData?.office || tmData?.registry_office),
  ].filter(Boolean);

  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="px-5 pt-4 pb-2 flex-shrink-0 flex items-center gap-3 text-[13px] text-muted-foreground">
        <button
          type="button"
          onClick={onClose}
          className="flex items-center gap-1.5 hover:text-foreground"
        >
          <ArrowLeft size={14} /> Back
        </button>
        <span className="text-border">|</span>
        <Link to={breadcrumbTo} className="hover:text-foreground">{breadcrumbLabel}</Link>
        <ChevronRight size={13} />
        <span className="text-foreground font-medium">{tmName}</span>
      </div>

      <div className="flex items-start justify-between gap-4 px-5 pb-4 flex-shrink-0 flex-wrap">
        <div className="flex items-start gap-3 min-w-0">
          {avatar}
          <div>
            <h2 className="text-[19px] font-semibold m-0">{tmName}</h2>
            <div className="flex items-center gap-2 mt-1.5 flex-wrap">
              <span className="app-no-link">#{tm.app_no}</span>
              <span className={`pill ${statusClass(tmStatus)}`}><span className="dot" />{tmStatus}</span>
            </div>
            {summaryBits.length > 0 && (
              <div className="text-[12.5px] text-muted-foreground mt-1.5">{summaryBits.join(' · ')}</div>
            )}
          </div>
        </div>
        {toolbar}
      </div>

      {refreshBanner}

      {renewalDate && (
        <div className="mx-5 mb-4 flex-shrink-0 flex items-center gap-2.5 px-3.5 py-2.5 rounded-md bg-status-info-bg text-status-info text-[13px]">
          <CalendarClock size={16} className="flex-shrink-0" />
          <span><b className="font-semibold">Renewal due:</b> {tmData.valid_upto}</span>
        </div>
      )}

      <div className="px-5 flex items-center gap-1 border-b border-border flex-shrink-0 flex-wrap">
        {PAGE_TABS.map((tab) => (
          <button
            key={tab}
            type="button"
            onClick={() => setActiveTab(tab)}
            className={`px-3 py-2 text-[13px] border-b-2 -mb-px ${activeTab === tab
              ? 'border-primary text-primary font-medium'
              : 'border-transparent text-muted-foreground hover:text-foreground'
              }`}
          >
            {tab}
          </button>
        ))}
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto px-5 py-4.5 max-w-[900px]">
        {activeTab === 'Overview' && overviewPanel}
        {activeTab === 'Timeline' && timelinePanel}
        {activeTab === 'Documents' && documentsPanel}
        {activeTab === 'Notes' && notesPanel}
        {activeTab === 'Tasks' && tasksPanel}
      </div>
      {viewerDoc && (
        <DocumentViewerModal url={viewerDoc.url} title={viewerDoc.title} onClose={() => setViewerDoc(null)} />
      )}
    </div>
  );
}

function parseDate(value: string | undefined | null): boolean {
  if (!value) return false;
  const d = new Date(value);
  return !isNaN(d.getTime());
}

function Field({ label, value, mono, col }: { label: string; value: string; mono?: boolean; col?: 'left' | 'right' }) {
  return (
    <div
      className={col === 'left' ? 'pb-2.5 pr-5' : 'pb-2.5 pl-5'}
      style={{
        borderBottom: '1px dashed var(--color-border)',
        borderRight: col === 'left' ? '1px dashed var(--color-border)' : undefined,
      }}
    >
      <div className="text-[12px] text-muted-foreground mb-0.5">{label}</div>
      <div className={`text-[13.5px] ${mono ? 'font-mono' : ''}`}>{value}</div>
    </div>
  );
}
