import { useCallback, useEffect, useState } from 'react';
import { ChevronLeft, ChevronRight, Plus } from 'lucide-react';
import { ActionRow, Drawer, Facts, PageIntro, Section, timeAgo, Tone } from './ui';

type Props = {
  api: (path: string, method?: string, body?: any) => Promise<any>;
  goTo: (page: string) => void;
};

type Tonal = 'good' | 'bad' | 'warn' | '';
const STATE: Record<string, { label: string; tone: Tonal }> = {
  queued: { label: 'Waiting for a machine', tone: '' },
  reserved: { label: 'Being scraped', tone: 'good' },
  retry_wait: { label: 'Retrying later', tone: 'warn' },
  failed: { label: 'Gave up', tone: 'bad' },
  completed: { label: 'Done', tone: 'good' },
  cancelled: { label: 'Cancelled', tone: '' },
};

const REASON: Record<string, string> = {
  scheduled: 'Its refresh date arrived', initial: 'A user added it', manual: 'Someone asked for a refresh',
  staff_request: 'Queued by staff', operator_retry: 'Retried by staff',
};

const VIEWS: [string, string][] = [['attention', 'Needs attention'], ['all', 'Everything'], ['cancelled', 'Cancelled']];

export function priorityWord(p: number): string {
  return p >= 300 ? 'Top' : p >= 150 ? 'High' : p >= 100 ? 'Normal' : p > 0 ? 'Low' : 'Scheduled';
}

export function friendlyError(raw: string | null | undefined): string {
  const text = (raw || '').trim();
  if (!text) return '';
  if (/offline reservation expired/i.test(text)) return "The machine did not report back in time, so the job went back in the queue.";
  if (/partial result/i.test(text)) return 'Some documents or panels were missing from the result, so it will be tried again.';
  if (/reservation too short/i.test(text)) return 'The machine ran out of time before it could start this one.';
  if (/extraction failed/i.test(text)) return 'The documents were saved but their text could not be read.';
  if (/portal processing failed/i.test(text)) return 'The portal did not return a usable answer.';
  if (/timed? ?out/i.test(text)) return 'The portal took too long to answer.';
  return text;
}

function when(value: string | null): string {
  if (!value) return '—';
  const seconds = Math.round((Date.parse(value) - Date.now()) / 1000);
  if (seconds <= 30) return 'any moment now';
  return seconds < 3600 ? `in ${Math.round(seconds / 60)} min` : `in ${Math.round(seconds / 3600)} h`;
}

function detail(job: any, max: number): string {
  switch (job.state) {
    case 'queued': return `Waiting ${timeAgo(job.created_at)}`;
    case 'reserved': return `${job.worker_name || 'A machine'}, started ${timeAgo(job.reserved_at)}`;
    case 'retry_wait': return `Next try ${when(job.available_at)}`;
    case 'failed': return friendlyError(job.last_error) || `Gave up after ${max} tries`;
    case 'completed': return `${job.worker_name || 'A machine'}, ${timeAgo(job.completed_at)}`;
    default: return timeAgo(job.completed_at);
  }
}

export default function WorkQueue({ api, goTo }: Props) {
  const [view, setView] = useState('attention');
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState('');
  const [adding, setAdding] = useState(false);
  const [open, setOpen] = useState<any>(null);

  const load = useCallback(async () => {
    try { setData(await api(`/queue-overview?view=${view}&limit=50&offset=${offset}`)); setError(''); } catch (err) { setError((err as Error).message); }
  }, [api, view, offset]);
  useEffect(() => { load(); const t = setInterval(load, 10000); return () => clearInterval(t); }, [load]);

  const counts = data?.counts || {};
  const items: any[] = data?.items || [];
  const max = data?.max_attempts || 5;
  const choose = (next: string) => { setView(next); setOffset(0); };
  const tiles: [string, string, number, Tonal][] = [
    ['waiting', 'Waiting', counts.waiting ?? 0, ''], ['active', 'Being scraped', counts.active ?? 0, ''],
    ['retrying', 'Retrying later', counts.retrying ?? 0, ''], ['failed', 'Gave up', counts.failed ?? 0, (counts.failed ?? 0) > 0 ? 'bad' : ''],
    ['done', 'Done in the last 24 h', counts.done_recently ?? 0, ''],
  ];
  const viewName = [...VIEWS, ...tiles.map(([k, l]) => [k, l] as [string, string])].find(([k]) => k === view)?.[1] || 'Jobs';

  return <>
    <PageIntro action={<button className="ops-button" onClick={() => setAdding(true)}><Plus size={15} />Queue trademarks</button>}>
      Every job here is one trademark waiting to be refreshed from the IP India portal. Jobs appear when a trademark's refresh date
      arrives, when a user adds or refreshes one, or when you queue one yourself. Machines take the highest priority first.
    </PageIntro>
    {error && <div className="ops-alert bad" role="alert">{error}</div>}
    <div className="ops-stats five">
      {tiles.map(([key, label, n, tone]) => <button key={key} className={`ops-stat ops-stat-button ${view === key ? 'on' : ''} ${tone}`} onClick={() => choose(key)}>
        <span>{label}</span><strong>{n}</strong></button>)}
    </div>
    <section className="ops-card ops-table-card">
      <div className="ops-card-heading">
        <h2>{viewName}</h2>
        <span className="ops-muted">{data ? `${data.total} job${data.total === 1 ? '' : 's'}` : 'Loading…'}</span>
      </div>
      <div className="ops-chips">{VIEWS.map(([key, label]) => <button key={key} className={`ops-chip ${view === key ? 'on' : ''}`} onClick={() => choose(key)}>{label}{key === 'cancelled' && counts.cancelled ? ` (${counts.cancelled})` : ''}</button>)}</div>
      <div className="ops-table-wrap"><table>
        <thead><tr><th>Trademark</th><th>Status</th><th>Details</th><th>Priority</th><th>Tries</th><th /></tr></thead>
        <tbody>{items.map(job => {
          const st = STATE[job.state] || { label: job.state, tone: '' as Tonal };
          return <tr key={job.id} className="ops-clickable" onClick={() => setOpen(job)}>
            <td><strong>{job.app_no}</strong>{job.mark && <div className="ops-muted">{job.mark}</div>}</td>
            <td><Tone tone={st.tone}>{st.label}</Tone></td>
            <td className="ops-wrap">{detail(job, max)}</td>
            <td>{priorityWord(job.priority)}</td>
            <td>{job.attempts} of {max}</td>
            <td><button className="ops-button secondary" onClick={e => { e.stopPropagation(); setOpen(job); }}>Manage</button></td>
          </tr>;
        })}</tbody>
      </table>
      {!items.length && <div className="ops-empty"><h3>{view === 'attention' ? 'Nothing needs attention' : 'No jobs here'}</h3>
        <p>{view === 'attention' ? 'No trademark is waiting, being scraped, retrying or failed. Machines will pick up work as it becomes due.' : 'Try another view above.'}</p></div>}
      </div>
      {data && data.total > 50 && <div className="ops-pagination"><span>{offset + 1}–{offset + items.length} of {data.total}</span>
        <div><button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))} aria-label="Previous"><ChevronLeft size={16} /></button>
          <button disabled={offset + items.length >= data.total} onClick={() => setOffset(offset + 50)} aria-label="Next"><ChevronRight size={16} /></button></div></div>}
    </section>
    {adding && <QueueTrademarks api={api} reload={load} onClose={() => setAdding(false)} onShow={() => { setAdding(false); choose('attention'); }} />}
    {open && <JobDetails job={open} max={max} api={api} reload={load} goTo={goTo} onClose={() => setOpen(null)} />}
  </>;
}

function QueueTrademarks({ api, reload, onClose, onShow }: { api: Props['api']; reload: () => Promise<void>; onClose: () => void; onShow: () => void }) {
  const [numbers, setNumbers] = useState('');
  const [priority, setPriority] = useState('normal');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<any>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true); setError(''); setResult(null);
    try {
      const r = await api('/queue', 'POST', { app_nos: numbers, priority });
      setResult(r);
      if (r.queued.length || r.already_waiting.length) setNumbers(r.invalid.join('\n'));
      await reload();
    } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  }
  const levels: [string, string, string][] = [['normal', 'Normal', 'Goes ahead of the daily scheduled refreshes.'], ['high', 'High', 'For a client who is waiting.'], ['top', 'Top', 'Goes first, ahead of everything else in the queue.']];

  return <Drawer title="Queue trademarks" subtitle="Refresh trademarks now instead of waiting for their date" onClose={onClose}>
    <form className="ops-stack" onSubmit={submit}>
      {error && <div className="ops-alert bad" role="alert">{error}</div>}
      {result && <div className={`ops-alert ${result.queued.length || result.already_waiting.length ? 'good' : 'bad'}`} role="status">
        {result.queued.length > 0 && <div><b>{result.queued.length}</b> queued.</div>}
        {result.already_waiting.length > 0 && <div><b>{result.already_waiting.length}</b> already in the queue, moved up to this priority.</div>}
        {result.invalid.length > 0 && <div><b>{result.invalid.length}</b> not valid application numbers: {result.invalid.join(', ')}. They are left in the box.</div>}
        {(result.queued.length > 0 || result.already_waiting.length > 0) && <button type="button" className="ops-text-button" onClick={onShow}>See them in the queue</button>}
      </div>}
      <label className="ops-field">Application numbers
        <textarea className="ops-textarea" value={numbers} onChange={e => setNumbers(e.target.value)} rows={7} placeholder={'7936098\n7931056\n7918928'} required autoFocus />
        <span className="ops-muted">One per line, or separated by spaces or commas. You can paste a whole column from Excel. Up to 500 at a time.</span>
      </label>
      <fieldset className="ops-radios"><legend>How urgent is it?</legend>
        {levels.map(([key, label, text]) => <label key={key} className="ops-radio"><input type="radio" name="priority" checked={priority === key} onChange={() => setPriority(key)} /><span><b>{label}</b><span className="ops-muted"> {text}</span></span></label>)}
      </fieldset>
      <button className="ops-button" disabled={busy || !numbers.trim()}>{busy ? 'Queuing…' : 'Queue trademarks'}</button>
    </form>
  </Drawer>;
}

function JobDetails({ job, max, api, reload, goTo, onClose }: { job: any; max: number; api: Props['api']; reload: () => Promise<void>; goTo: Props['goTo']; onClose: () => void }) {
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const st = STATE[job.state] || { label: job.state, tone: '' as Tonal };

  async function run(label: string, body: any, confirmText?: string) {
    if (confirmText && !window.confirm(confirmText)) return;
    setBusy(true); setError(''); setNotice('');
    try { await api(`/jobs/${job.id}`, 'PATCH', body); setNotice(label); await reload(); } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  }

  const sentence: Record<string, string> = {
    queued: 'It is waiting for a machine to pick it up. Higher priority jobs are taken first.',
    reserved: `${job.worker_name || 'A machine'} has taken it and is scraping now. If that machine stops, the job returns to the queue by itself.`,
    retry_wait: 'The last try did not finish. It will be tried again automatically after a short wait.',
    failed: `It failed ${max} times, so the system stopped trying. Use Retry now once the cause is fixed.`,
    completed: 'It finished and the result was saved.',
    cancelled: 'It was cancelled by staff.',
  };
  const waiting = job.state === 'queued' || job.state === 'retry_wait';

  return <Drawer title={job.app_no} subtitle={<><Tone tone={st.tone}>{st.label}</Tone>{job.mark && <span> {job.mark}</span>}</>} onClose={onClose}>
    {error && <div className="ops-alert bad" role="alert">{error}</div>}
    {notice && <div className="ops-alert good" role="status">{notice}</div>}
    <Section title="What is happening"><p>{sentence[job.state] || ''}</p></Section>
    {job.last_error && <Section title="Last problem"><p>{friendlyError(job.last_error)}</p>{friendlyError(job.last_error) !== job.last_error && <p className="ops-muted">Details: {job.last_error}</p>}</Section>}
    <Section title="Details">
      <Facts items={[
        ['Why it is here', REASON[job.reason] || job.reason],
        ['Priority', `${priorityWord(job.priority)} (${job.priority})`],
        ['Tries', `${job.attempts} of ${max}`],
        ['Machine', job.state === 'queued' || job.state === 'cancelled' ? '—' : job.worker_name || '—'],
        ['Queued', timeAgo(job.created_at)],
        ['Started', job.reserved_at ? timeAgo(job.reserved_at) : '—'],
        ['Finished', job.completed_at ? timeAgo(job.completed_at) : '—'],
        ['Full re-download', job.force_full ? 'Yes' : 'No'],
      ]} />
    </Section>
    <Section title="Manage">
      {job.state === 'reserved'
        ? <p className="ops-muted">A machine is working on this right now, so it can't be retried yet. If the machine is stuck you can cancel the job, or pause the machine in <button className="ops-text-button" onClick={() => { onClose(); goTo('workers'); }}>Fleet machines</button>.</p>
        : <ActionRow title="Retry now" text={waiting ? 'Puts it at the front and gives it a fresh set of tries.' : 'Starts a new job for this trademark right away.'}><button className="ops-button" disabled={busy} onClick={() => run('Queued again, at high priority.', { action: 'retry', priority: 150 })}>Retry now</button></ActionRow>}
      {waiting && <ActionRow title="Move to the front" text="Makes it the next one a machine takes."><button className="ops-button secondary" disabled={busy} onClick={() => run('Moved to the front.', { action: 'priority', priority: 500 })}>Move to front</button></ActionRow>}
      {job.state !== 'reserved' && <ActionRow title="Re-download everything" text="Retries it and downloads all of its documents again, ignoring files already saved. Slower, but fixes missing or damaged documents."><button className="ops-button secondary" disabled={busy} onClick={() => run('Queued with a full re-download.', { action: 'force_full', priority: 150 })}>Re-download all</button></ActionRow>}
      {job.state !== 'completed' && job.state !== 'cancelled' && <ActionRow title="Cancel" text={job.state === 'reserved' ? "Stops it. The machine's result for it will be set aside." : 'Removes it from the queue. The trademark keeps its last saved data.'}>
        <button className="ops-button danger" disabled={busy} onClick={() => run('Cancelled.', { action: 'cancel' }, job.state === 'reserved' ? `${job.worker_name || 'A machine'} is working on this right now. Cancel it anyway?` : undefined)}>Cancel job</button></ActionRow>}
    </Section>
    <details className="ops-tech"><summary>Technical details</summary><pre>{JSON.stringify(job, null, 2)}</pre></details>
  </Drawer>;
}
