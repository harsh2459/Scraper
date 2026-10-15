import { useState, useEffect, useMemo, useCallback } from 'react';
import Ring from '../components/tm/Ring';
import { API_BASE } from '../lib/api';
import Pagination from '../components/Pagination';
import TrademarkOverlay from '../components/TrademarkOverlay';
import AddTaskModal from '../components/AddTaskModal';
import ComplianceReview, { EVENT_LABELS } from '../components/ComplianceReview';
import { Calendar as CalendarIcon, List, ChevronLeft, ChevronRight, Plus, Trash2 } from 'lucide-react';

const PAGE_SIZE = 100;

const STATUS_LABELS: Record<string, string> = {
  pending: 'Pending',
  in_progress: 'In Progress',
  completed: 'Completed',
  closed: 'Closed',
};

const COMPLIANCE_LABELS: Record<string, string> = {
  ...EVENT_LABELS,
  examination_report_reply: 'Examination Report Reply',
  reply_us_132: 'Reply Exm Rpt u/s 132',
  hearing_show_cause: 'Hearing (Show Cause)',
  counter_statement: 'Counter Statement',
};

const COMPLIANCE_CLASSES: Record<string, string> = {
  examination_report_reply: 'bg-status-warn-bg border-status-warn/30 text-status-warn',
  reply_us_132: 'bg-status-warn-bg border-status-warn/30 text-status-warn',
  hearing_show_cause: 'bg-status-bad-bg border-status-bad/30 text-status-bad',
  counter_statement: 'bg-status-info-bg border-status-info/30 text-status-info',
};

const STATUS_CYCLE: Record<string, string> = {
  pending: 'in_progress',
  in_progress: 'completed',
  completed: 'closed',
  closed: 'pending',
};

function TaskStatusPill({ status, onClick }: { status: string; onClick: () => void }) {
  const cls =
    status === 'completed' ? 'bg-status-good-bg text-status-good' :
    status === 'in_progress' ? 'bg-status-warn-bg text-status-warn' :
    status === 'closed' ? 'bg-muted text-muted-foreground' :
    'bg-status-info-bg text-status-info';
  return (
    <button type="button" onClick={onClick} className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ${cls}`}>
      {STATUS_LABELS[status] || status}
    </button>
  );
}

export default function Tasks() {
  const [data, setData] = useState<any>(null);
  const [page, setPage] = useState(1);
  const [viewMode, setViewMode] = useState<'calendar' | 'list'>('calendar');
  const [openTmId, setOpenTmId] = useState<number | null>(null);
  const [currentMonth, setCurrentMonth] = useState(() => {
    const now = new Date();
    return new Date(now.getFullYear(), now.getMonth(), 1);
  });
  const [addTaskDate, setAddTaskDate] = useState<string | null>(null);
  const [category, setCategory] = useState('');
  const [side, setSide] = useState('');
  const [eventOffset, setEventOffset] = useState(0);

  const loadData = useCallback(() => {
    const iso = (d: Date) => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    const params = new URLSearchParams({start: iso(currentMonth), end: iso(new Date(currentMonth.getFullYear(), currentMonth.getMonth()+1, 0)), limit: '200', offset: String(eventOffset)});
    if (category) params.set('category', category);
    if (side) params.set('side', side);
    fetch(`${API_BASE}/api/tasks-compliances?${params}`)
      .then((res) => res.json())
      .then((json) => setData(json))
      .catch((err) => console.error(err));
  }, [currentMonth, category, side, eventOffset]);

  useEffect(() => setEventOffset(0), [currentMonth, category, side]);

  useEffect(() => { loadData(); }, [loadData]);

  const allReminders = useMemo(() => data?.reminders ?? [], [data]);
  const allTasks = useMemo(() => data?.custom_tasks ?? [], [data]);
  const compliance = useMemo(() => data?.compliance ?? {}, [data]);
  const allComplianceItems = useMemo(() => {
    const items: any[] = [];
    Object.entries(compliance).forEach(([category, list]: [string, any]) => {
      (list || []).forEach((i: any) => items.push({ ...i, category }));
    });
    return items;
  }, [compliance]);

  const cycleTaskStatus = async (task: any) => {
    const next = STATUS_CYCLE[task.status] || 'pending';
    setData((prev: any) => ({
      ...prev,
      custom_tasks: prev.custom_tasks.map((t: any) => (t.id === task.id ? { ...t, status: next } : t)),
    }));
    try {
      await fetch(`${API_BASE}/api/tasks/${task.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: next }),
      });
    } catch {
      loadData();
    }
  };

  const deleteTask = async (task: any) => {
    if (!window.confirm(`Delete task "${task.title}"?`)) return;
    setData((prev: any) => ({ ...prev, custom_tasks: prev.custom_tasks.filter((t: any) => t.id !== task.id) }));
    try {
      await fetch(`${API_BASE}/api/tasks/${task.id}`, { method: 'DELETE' });
    } catch {
      loadData();
    }
  };

  const totalPages = Math.max(1, Math.ceil(allReminders.length / PAGE_SIZE));
  const pageReminders = useMemo(
    () => allReminders.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE),
    [allReminders, page],
  );

  const complianceByDate = useMemo(() => {
    const map = new Map<string, any[]>();
    allComplianceItems.forEach((i: any) => {
      if (!i.due_date) return;
      const key = String(i.due_date).slice(0, 10);
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(i);
    });
    return map;
  }, [allComplianceItems]);

  // custom_tasks.due_date comes back as a real ISO date (YYYY-MM-DD) from
  // Postgres, so no DD/MM/YYYY parsing is needed here unlike reminders.
  const tasksByDate = useMemo(() => {
    const map = new Map<string, any[]>();
    allTasks.forEach((t: any) => {
      const key = String(t.due_date).slice(0, 10);
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(t);
    });
    return map;
  }, [allTasks]);

  // Count per YYYY-MM so we can show which months actually have compliance items.
  const complianceByMonth = useMemo(() => {
    const m = new Map<string, number>();
    allComplianceItems.forEach((i: any) => {
      if (!i.due_date) return;
      const k = String(i.due_date).slice(0, 7);
      m.set(k, (m.get(k) || 0) + 1);
    });
    return m;
  }, [allComplianceItems]);

  const monthKey = `${currentMonth.getFullYear()}-${String(currentMonth.getMonth() + 1).padStart(2, '0')}`;
  const tasksThisMonth = useMemo(
    () => allTasks.filter((t: any) => String(t.due_date).slice(0, 7) === monthKey),
    [allTasks, monthKey],
  );
  const completedThisMonth = tasksThisMonth.filter((t: any) => t.status === 'completed').length;
  const statusCounts = useMemo(() => {
    const counts: Record<string, number> = { pending: 0, in_progress: 0, completed: 0, closed: 0 };
    allTasks.forEach((t: any) => { counts[t.status] = (counts[t.status] || 0) + 1; });
    return counts;
  }, [allTasks]);

  if (!data) return <div className="loading"><Ring state="loading" size={28} /><span>Loading tasks…</span></div>;

  const prevMonth = () => setCurrentMonth(new Date(currentMonth.getFullYear(), currentMonth.getMonth() - 1, 1));
  const nextMonth = () => setCurrentMonth(new Date(currentMonth.getFullYear(), currentMonth.getMonth() + 1, 1));

  const daysInMonth = new Date(currentMonth.getFullYear(), currentMonth.getMonth() + 1, 0).getDate();
  const firstDay = new Date(currentMonth.getFullYear(), currentMonth.getMonth(), 1).getDay();

  const blanks = Array.from({ length: firstDay }).map((_, i) => <div key={`blank-${i}`} className="bg-muted/20 border-r border-b border-border min-h-[110px]" />);
  const days = Array.from({ length: daysInMonth }).map((_, i) => {
    const day = i + 1;
    const date = new Date(currentMonth.getFullYear(), currentMonth.getMonth(), day);
    const key = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
    const dayCompliance = complianceByDate.get(key) || [];
    const dayTasks = tasksByDate.get(key) || [];
    const isToday = new Date().toDateString() === date.toDateString();

    return (
      <div key={key} className={`border-r border-b border-border min-h-[110px] p-2 flex flex-col gap-1.5 group ${isToday ? 'bg-[rgba(20,71,230,0.03)]' : 'bg-panel'}`}>
        <div className="flex items-center justify-between">
          <div className={`text-[12px] font-medium w-6 h-6 flex items-center justify-center rounded-full ${isToday ? 'bg-primary text-primary-foreground' : 'text-foreground'}`}>
            {day}
          </div>
          <button
            type="button"
            onClick={() => setAddTaskDate(key)}
            className="w-5 h-5 rounded flex items-center justify-center text-muted-foreground opacity-0 group-hover:opacity-100 hover:bg-muted hover:text-foreground transition-opacity"
            title="Add task"
          >
            <Plus size={13} />
          </button>
        </div>
        <div className="flex flex-col gap-1 flex-1 overflow-y-auto [&::-webkit-scrollbar]:hidden">
          {dayCompliance.map((c: any, idx: number) => (
            <div
              key={`c-${idx}`}
              className={`border rounded px-1.5 py-1 text-[11px] leading-tight ${COMPLIANCE_CLASSES[c.category] || 'bg-muted border-border'}`}
              title={c.subject}
            >
              <div className="font-semibold truncate flex items-center gap-1">
                {COMPLIANCE_LABELS[c.category] || c.category}
                {c.days_until < 0 && <span className="text-[9px] font-normal opacity-80">(overdue)</span>}
                {c.is_estimated && <span className="text-[9px] font-normal opacity-70">(Est.)</span>}
              </div>
              <button type="button" onClick={() => setOpenTmId(c.tm_id)} className="text-foreground/80 truncate hover:underline block text-left">
                {c.word_mark || c.label || c.app_no}
              </button>
            </div>
          ))}
          {dayTasks.map((t: any) => (
            <div
              key={`t-${t.id}`}
              className={`border rounded px-1.5 py-1 text-[11px] leading-tight ${
                t.status === 'completed' ? 'bg-status-good-bg border-status-good/30' :
                t.status === 'in_progress' ? 'bg-status-warn-bg border-status-warn/30' :
                t.status === 'closed' ? 'bg-muted border-border' :
                'bg-status-info-bg border-status-info/30'
              }`}
            >
              <div className="flex items-start justify-between gap-1">
                <button
                  type="button"
                  onClick={() => cycleTaskStatus(t)}
                  className={`font-semibold text-left truncate ${t.status === 'completed' ? 'line-through opacity-70' : ''}`}
                  title="Click to change status"
                >
                  {t.title}
                </button>
                <button type="button" onClick={() => deleteTask(t)} className="opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-status-bad flex-shrink-0">
                  <Trash2 size={11} />
                </button>
              </div>
              {t.app_no && (
                <button type="button" onClick={() => setOpenTmId(t.my_trademark_id)} className="text-muted-foreground truncate hover:text-primary block">
                  {t.word_mark || t.label || t.app_no}
                </button>
              )}
            </div>
          ))}
        </div>
      </div>
    );
  });

  const totalCells = blanks.length + days.length;
  const trailingBlanks = totalCells % 7 === 0 ? 0 : 7 - (totalCells % 7);
  const trailing = Array.from({ length: trailingBlanks }).map((_, i) => <div key={`trail-${i}`} className="bg-muted/20 border-r border-b border-border min-h-[110px]" />);

  return (
    <div className="flex-1 min-h-0 flex flex-col overflow-hidden">
      <div className="px-5 py-2.5 flex-shrink-0 flex items-center justify-between gap-4 flex-wrap border-b border-border">
        {allTasks.length > 0 ? (
          <div className="flex items-center gap-3 flex-wrap min-w-0 flex-1">
            <span className="text-[13px] font-medium whitespace-nowrap">
              {completedThisMonth}/{tasksThisMonth.length} tasks completed in {currentMonth.toLocaleString('default', { month: 'long' })}
            </span>
            <div className="h-1.5 w-24 rounded-full bg-muted overflow-hidden flex-shrink-0">
              <div
                className="h-full bg-status-good rounded-full transition-[width]"
                style={{ width: `${tasksThisMonth.length ? (completedThisMonth / tasksThisMonth.length) * 100 : 0}%` }}
              />
            </div>
            <div className="flex items-center gap-1.5 flex-wrap">
              {Object.entries(STATUS_LABELS).map(([key, label]) => (
                <span key={key} className="inline-flex items-center gap-1 rounded-full border border-border px-2 py-0.5 text-[11px] text-muted-foreground">
                  {label} <span className="font-mono font-medium text-foreground">{statusCounts[key] || 0}</span>
                </span>
              ))}
            </div>
          </div>
        ) : (
          <h1 className="text-[15px] font-semibold m-0">Tasks &amp; Compliances</h1>
        )}
        <div className="flex items-center gap-2 flex-shrink-0">
          <button type="button" className="btn btn-accent" onClick={() => setAddTaskDate(new Date().toISOString().slice(0, 10))}>
            <Plus size={13} /> Add task
          </button>
          <div className="flex items-center p-0.5 rounded-md bg-muted border border-border">
            <button onClick={() => setViewMode('calendar')} className={`flex items-center gap-1.5 px-3 py-1.5 rounded-[4px] text-[12.5px] font-medium transition-all ${viewMode === 'calendar' ? 'bg-panel shadow-sm text-foreground' : 'text-muted-foreground hover:text-foreground'}`}>
              <CalendarIcon size={14} /> Calendar
            </button>
            <button onClick={() => setViewMode('list')} className={`flex items-center gap-1.5 px-3 py-1.5 rounded-[4px] text-[12.5px] font-medium transition-all ${viewMode === 'list' ? 'bg-panel shadow-sm text-foreground' : 'text-muted-foreground hover:text-foreground'}`}>
              <List size={14} /> List
            </button>
          </div>
        </div>
      </div>

      <div className="px-5 py-3 flex gap-3 items-center flex-wrap">
        <select className="input" aria-label="Compliance type" value={category} onChange={e => setCategory(e.target.value)}><option value="">All compliance types</option>{Object.entries(COMPLIANCE_LABELS).map(([key,label]) => <option key={key} value={key}>{label}</option>)}</select>
        <select className="input" aria-label="Portfolio side" value={side} onChange={e => setSide(e.target.value)}><option value="">Both portfolio sides</option><option value="own_mark">My marks</option><option value="opposition_out">Oppositions I filed</option></select>
        {viewMode === 'list' && data.event_page && <span className="text-[12px] text-muted-foreground">{data.event_page.total} events · showing {eventOffset + 1}–{eventOffset + allComplianceItems.length}</span>}
        {viewMode === 'list' && eventOffset > 0 && <button className="btn" onClick={() => setEventOffset(Math.max(0,eventOffset-200))}>Previous events</button>}
        {viewMode === 'list' && data.event_page?.total > eventOffset + 200 && <button className="btn" onClick={() => setEventOffset(eventOffset+200)}>More events</button>}
      </div>
      {viewMode === 'list' && allComplianceItems.length > 0 && (
        <section className="mx-5 mb-3 flex flex-col gap-2">
          <h2 className="text-[13px] font-semibold">Compliance events</h2>
          {allComplianceItems.filter((event: any) => event.source === 'event').map((event: any) => <ComplianceReview key={`${event.source}-${event.source_id}-${event.tm_id}`} event={event} onSaved={loadData} />)}
        </section>
      )}
      {viewMode === 'calendar' ? (
        <div className="flex-1 min-h-0 overflow-y-auto px-5 pb-5">
          <div className="table-panel flex flex-col bg-panel">
            <div className="flex items-center justify-between px-4 py-3 border-b border-border bg-muted/30 flex-wrap gap-2">
              <div className="flex items-center gap-2">
                <h2 className="text-[15px] font-semibold">{currentMonth.toLocaleString('default', { month: 'long', year: 'numeric' })}</h2>
                {(() => {
                  const n = complianceByMonth.get(monthKey) || 0;
                  return <span className="text-[12px] text-muted-foreground">{n} compliance item{n === 1 ? '' : 's'} this month</span>;
                })()}
              </div>
              <div className="flex items-center gap-1">
                <button onClick={prevMonth} className="p-1 rounded hover:bg-border text-muted-foreground hover:text-foreground transition-colors"><ChevronLeft size={18} /></button>
                <button onClick={() => setCurrentMonth(new Date(new Date().getFullYear(), new Date().getMonth(), 1))} className="px-2 py-1 text-[12px] font-medium rounded hover:bg-border text-foreground mx-1">Today</button>
                <button onClick={nextMonth} className="p-1 rounded hover:bg-border text-muted-foreground hover:text-foreground transition-colors"><ChevronRight size={18} /></button>
              </div>
            </div>
            <div className="grid grid-cols-7 border-b border-border">
              {['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].map(d => (
                <div key={d} className="py-2 text-center text-[12px] font-semibold text-muted-foreground border-r border-border last:border-r-0">{d}</div>
              ))}
            </div>
            <div className="grid grid-cols-7">
              {blanks}
              {days}
              {trailing}
            </div>
          </div>
        </div>
      ) : (
        <div className="flex-1 min-h-0 overflow-y-auto px-5 flex flex-col gap-6 pb-5">
          {allTasks.length > 0 && (
            <div>
              <h3 className="text-[13.5px] font-semibold mb-2">My Tasks</h3>
              <div className="table-panel">
                <table className="w-full border-separate border-spacing-0">
                  <thead>
                    <tr>
                      <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Title</th>
                      <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Linked Trademark</th>
                      <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Due Date</th>
                      <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Status</th>
                      <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {allTasks.map((t: any) => (
                      <tr key={t.id} className="hover:bg-muted">
                        <td style={{ borderBottom: '1px dashed var(--color-border)' }} className={`px-2.5 py-2 text-[13px] font-medium ${t.status === 'completed' ? 'line-through opacity-70' : ''}`}>{t.title}</td>
                        <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2 text-[13px]">
                          {t.app_no ? (
                            <button type="button" onClick={() => setOpenTmId(t.my_trademark_id)} className="app-no-link">{t.word_mark || t.label || t.app_no}</button>
                          ) : '—'}
                        </td>
                        <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2 text-[12.5px] text-muted-foreground font-mono">{String(t.due_date).slice(0, 10)}</td>
                        <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2 text-[13px]"><TaskStatusPill status={t.status} onClick={() => cycleTaskStatus(t)} /></td>
                        <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2 text-[12.5px]">
                          <button type="button" onClick={() => deleteTask(t)} className="text-muted-foreground hover:text-status-bad"><Trash2 size={13} /></button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          <div>
            <h3 className="text-[13.5px] font-semibold mb-2">Registry Reminders</h3>
            {allReminders.length ? (
              <>
                <div className="table-panel">
                  <table className="w-full border-separate border-spacing-0">
                    <thead>
                      <tr>
                        <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">App. Num</th>
                        <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Word Mark</th>
                        <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Reminder</th>
                        <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Despatch Date</th>
                      </tr>
                    </thead>
                    <tbody>
                      {pageReminders.map((r: any, idx: number) => (
                        <tr key={idx} className="cursor-pointer hover:bg-muted" onClick={() => setOpenTmId(r.tm_id)}>
                          <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2 text-[13px]"><span className="app-no-link">{r.app_no}</span></td>
                          <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2 text-[13px]">{r.word_mark || r.label || '—'}</td>
                          <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2 text-[13px] font-medium">{r.subject}</td>
                          <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2 text-[12.5px] text-muted-foreground font-mono">{r.despatch_date || r.corres_date || '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <Pagination
                  page={page}
                  totalPages={totalPages}
                  totalRows={allReminders.length}
                  pageRows={PAGE_SIZE}
                  rowLabel="reminders"
                  onPrev={() => setPage((p) => Math.max(1, p - 1))}
                  onNext={() => setPage((p) => Math.min(totalPages, p + 1))}
                />
              </>
            ) : (
              <div className="py-10 text-center text-muted-foreground text-[13.5px]">No reminder notices on file yet.</div>
            )}
          </div>
        </div>
      )}
      {openTmId && <TrademarkOverlay tmId={openTmId} onClose={() => setOpenTmId(null)} />}
      {addTaskDate && (
        <AddTaskModal
          defaultDate={addTaskDate}
          onClose={() => setAddTaskDate(null)}
          onCreated={loadData}
        />
      )}
    </div>
  );
}
