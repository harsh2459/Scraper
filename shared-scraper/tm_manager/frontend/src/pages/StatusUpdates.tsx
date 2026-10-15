import { useState, useEffect, useMemo, useRef } from 'react';
import Ring from '../components/tm/Ring';
import MarkThumb from '../components/MarkThumb';
import { Search, Filter, X } from 'lucide-react';
import { statusClass } from '../lib/status';
import { API_BASE } from '../lib/api';
import Pagination from '../components/Pagination';
import TrademarkOverlay from '../components/TrademarkOverlay';

const PAGE_SIZE = 100;

// changed_at comes back as an RFC 2822 string (e.g. "Mon, 24 Aug 2026
// 04:37:42 GMT") — this reduces it to the yyyy-mm-dd a native <input
// type=date> filter needs, in the browser's local timezone (same as the
// existing toLocaleDateString('en-GB') display below).
function toDateOnly(changedAt: string): string {
  if (!changedAt) return '';
  const d = new Date(changedAt);
  if (Number.isNaN(d.getTime())) return '';
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}


export default function StatusUpdates() {
  const [data, setData] = useState<any>(null);
  const [q, setQ] = useState('');
  const [fromStatusFilter, setFromStatusFilter] = useState('');
  const [toStatusFilter, setToStatusFilter] = useState('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [page, setPage] = useState(1);
  const [openTmId, setOpenTmId] = useState<number | null>(null);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const filtersRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fetch(`${API_BASE}/api/status-changes`)
      .then((res) => res.json())
      .then((json) => setData(json))
      .catch((err) => console.error(err));
  }, []);

  useEffect(() => {
    function onClickOutside(e: MouseEvent) {
      if (filtersRef.current && !filtersRef.current.contains(e.target as Node)) {
        setFiltersOpen(false);
      }
    }
    if (filtersOpen) document.addEventListener('mousedown', onClickOutside);
    return () => document.removeEventListener('mousedown', onClickOutside);
  }, [filtersOpen]);

  const availableFromStatuses = useMemo(() => {
    const set = new Set<string>((data?.changes || []).map((c: any) => c.old_status).filter(Boolean));
    return Array.from(set).sort();
  }, [data]);
  const availableToStatuses = useMemo(() => {
    const set = new Set<string>((data?.changes || []).map((c: any) => c.new_status).filter(Boolean));
    return Array.from(set).sort();
  }, [data]);

  const activeFilterCount = [fromStatusFilter, toStatusFilter, dateFrom, dateTo].filter(Boolean).length;
  const clearFilters = () => {
    setFromStatusFilter('');
    setToStatusFilter('');
    setDateFrom('');
    setDateTo('');
    setPage(1);
  };

  const filtered = useMemo(() => {
    if (!data?.changes) return [];
    const needle = q.trim().toLowerCase();
    return data.changes.filter((c: any) => {
      if (needle) {
        const matchesText =
          (c.word_mark || '').toLowerCase().includes(needle) ||
          (c.app_no || '').toLowerCase().includes(needle) ||
          (c.proprietor_name || '').toLowerCase().includes(needle);
        if (!matchesText) return false;
      }
      if (fromStatusFilter && c.old_status !== fromStatusFilter) return false;
      if (toStatusFilter && c.new_status !== toStatusFilter) return false;
      const changedDate = toDateOnly(c.changed_at);
      if (dateFrom && (!changedDate || changedDate < dateFrom)) return false;
      if (dateTo && (!changedDate || changedDate > dateTo)) return false;
      return true;
    });
  }, [data, q, fromStatusFilter, toStatusFilter, dateFrom, dateTo]);

  // Reset to page 1 when search or filters change
  const prevQ = useMemo(() => q, [q]);
  useMemo(() => { if (prevQ !== q) setPage(1); }, [q]);

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const pageRows = useMemo(
    () => filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE),
    [filtered, page],
  );

  if (!data) return <div className="loading"><Ring state="loading" size={28} /><span>Loading updates…</span></div>;

  return (
    <div className="flex-1 min-h-0 flex flex-col overflow-hidden">
      <div className="flex items-center justify-between gap-3 px-5 py-3 border-b border-border bg-panel flex-wrap flex-shrink-0">
        <div className="font-display font-semibold text-[15px]">Status Changes</div>
        <div className="flex items-center gap-2 flex-1 min-w-[320px] justify-end">
          <div className="flex items-center border border-border-strong rounded-md overflow-hidden max-w-[360px] flex-1 min-w-[220px]">
            <span className="flex items-center px-2.5 text-muted-foreground"><Search size={13} /></span>
            <input
              type="text"
              value={q}
              onChange={(e) => { setQ(e.target.value); setPage(1); }}
              placeholder="Search mark, app number or proprietor…"
              className="border-none flex-1 text-[13px] py-1.5 pr-2.5 outline-none"
            />
          </div>
          <div className="relative" ref={filtersRef}>
            <button
              type="button"
              className={`btn ${activeFilterCount ? 'border-primary text-primary' : ''}`}
              onClick={() => setFiltersOpen((v) => !v)}
            >
              <Filter size={12} /> Filters{activeFilterCount ? ` (${activeFilterCount})` : ''}
            </button>
            {filtersOpen && (
              <div className="absolute right-0 top-[calc(100%+6px)] z-20 w-[280px] bg-panel border border-border rounded-lg shadow-lg p-4 flex flex-col gap-3.5">
                <div className="flex items-center justify-between">
                  <span className="text-[13px] font-semibold">Filters</span>
                  {activeFilterCount > 0 && (
                    <button type="button" className="text-[12px] text-muted-foreground hover:text-primary flex items-center gap-1" onClick={clearFilters}>
                      <X size={12} /> Clear all
                    </button>
                  )}
                </div>

                <label className="flex flex-col gap-1.5">
                  <span className="eyebrow text-muted-foreground!">From status</span>
                  <select
                    value={fromStatusFilter}
                    onChange={(e) => { setFromStatusFilter(e.target.value); setPage(1); }}
                    className="border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary"
                  >
                    <option value="">Any status</option>
                    {availableFromStatuses.map((s) => (
                      <option key={s} value={s}>{s}</option>
                    ))}
                  </select>
                </label>

                <label className="flex flex-col gap-1.5">
                  <span className="eyebrow text-muted-foreground!">To status</span>
                  <select
                    value={toStatusFilter}
                    onChange={(e) => { setToStatusFilter(e.target.value); setPage(1); }}
                    className="border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary"
                  >
                    <option value="">Any status</option>
                    {availableToStatuses.map((s) => (
                      <option key={s} value={s}>{s}</option>
                    ))}
                  </select>
                </label>

                <div className="flex flex-col gap-1.5">
                  <span className="eyebrow text-muted-foreground!">Changed date</span>
                  <div className="flex items-center gap-2">
                    <input
                      type="date"
                      value={dateFrom}
                      onChange={(e) => { setDateFrom(e.target.value); setPage(1); }}
                      className="border border-border-strong rounded-md px-2 py-1.5 text-[12.5px] outline-none focus:border-primary flex-1 min-w-0"
                    />
                    <span className="text-muted-foreground text-[12px]">to</span>
                    <input
                      type="date"
                      value={dateTo}
                      onChange={(e) => { setDateTo(e.target.value); setPage(1); }}
                      className="border border-border-strong rounded-md px-2 py-1.5 text-[12.5px] outline-none focus:border-primary flex-1 min-w-0"
                    />
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto">
        {filtered.length ? (
          <table className="w-full border-separate border-spacing-0">
            <thead>
              <tr>
                <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Trademark</th>
                <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Owner</th>
                <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">From</th>
                <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">To</th>
                <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Changed</th>
                <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Actions</th>
              </tr>
            </thead>
            <tbody>
              {pageRows.map((c: any) => (
                <tr key={`${c.id}-${c.changed_at}`} className="hover:bg-muted">
                  <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2 text-[13px]">
                    <div className="flex items-center gap-2.5">
                      <MarkThumb id={c.id} hasLogo={c.has_logo} name={c.word_mark} />
                      <div>
                        <button type="button" onClick={() => setOpenTmId(c.id)} className="app-no-link">{c.app_no}</button>
                        <div className="font-medium">{c.word_mark || '—'}</div>
                      </div>
                    </div>
                  </td>
                  <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2 text-[12.5px] text-muted-foreground max-w-[220px]">{c.proprietor_name || '—'}</td>
                  <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2 text-[13px]"><span className={`pill ${statusClass(c.old_status)}`}>{c.old_status}</span></td>
                  <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2 text-[13px]"><span className={`pill ${statusClass(c.new_status)}`}>{c.new_status}</span></td>
                  <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2 text-[12.5px] text-muted-foreground font-mono">
                    {c.changed_at ? new Date(c.changed_at).toLocaleDateString('en-GB') : '—'}
                  </td>
                  <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2 text-[12.5px]">
                    <button
                      type="button"
                      className="text-primary font-medium hover:underline"
                      onClick={() => alert('Email notifications aren\'t wired up yet.')}
                    >
                      Send Email &rarr;
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="py-10 text-center text-muted-foreground text-[13.5px]">
            {q || activeFilterCount ? 'No status changes match your search/filters.' : 'No status changes recorded yet — these appear once a trademark has been fetched at least twice with a different status.'}
          </div>
        )}
      </div>

      {filtered.length > 0 && (
        <Pagination
          page={page}
          totalPages={totalPages}
          totalRows={filtered.length}
          pageRows={PAGE_SIZE}
          rowLabel="changes"
          onPrev={() => setPage((p) => Math.max(1, p - 1))}
          onNext={() => setPage((p) => Math.min(totalPages, p + 1))}
        />
      )}
      {openTmId && <TrademarkOverlay tmId={openTmId} onClose={() => setOpenTmId(null)} />}
    </div>
  );
}
