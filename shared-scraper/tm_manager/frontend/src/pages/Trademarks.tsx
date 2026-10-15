import { useEffect, useState, useRef } from 'react';
import Ring from '../components/tm/Ring';
import MarkThumb from '../components/MarkThumb';
import { useSearchParams, useNavigate } from 'react-router-dom';
import { ChevronDown, ChevronUp, Columns3, Download, Filter, Search, Trash2, X } from 'lucide-react';
import { statusClass } from '../lib/status';
import { API_BASE } from '../lib/api';

// App Number is always shown first and isn't part of this list — every
// other column is opt-in/reorderable per user (see the Columns picker).
// `render` reads straight off the row object the backend returns (see
// db.list_trademarks) — kept together with the label so adding a new
// pickable column is ever only a one-line change here.
const COLUMN_DEFS: { key: string; label: string; render: (tm: any) => React.ReactNode }[] = [
  { key: 'word_mark', label: 'Word Mark', render: (tm) => (
      <div className="flex items-center gap-2.5">
        <MarkThumb id={tm.id} hasLogo={tm.has_logo} name={tm.word_mark || tm.label || tm.app_no} />
        <div className="text-[13.5px] whitespace-normal wrap-break-word">{tm.word_mark || tm.label || '—'}</div>
      </div>
    ) },
  { key: 'class_number', label: 'Class', render: (tm) => <span className="text-muted-foreground">{tm.class_number || '—'}</span> },
  { key: 'application_date', label: 'App. Date', render: (tm) => <span className="text-muted-foreground font-mono">{tm.application_date || '—'}</span> },
  { key: 'portal_status', label: 'Status', render: (tm) => (
      tm.portal_status ? (
        <span className={`pill ${statusClass(tm.portal_status)}`}><span className="dot" />{tm.portal_status}</span>
      ) : (
        <span className="pill s-default"><span className="dot" />{tm.status === 'error' ? 'Fetch failed' : tm.status === 'fetching' ? 'Fetching…' : 'Pending fetch'}</span>
      )
    ) },
  { key: 'proprietor_name', label: 'Proprietor', render: (tm) => <span className="max-w-55 block truncate">{tm.proprietor_name || '—'}</span> },
  { key: 'sub_status', label: 'Sub Status', render: (tm) => tm.sub_status || '—' },
  { key: 'tm_type', label: 'TM Type', render: (tm) => tm.tm_type || '—' },
  { key: 'state', label: 'State', render: (tm) => tm.state || '—' },
  { key: 'valid_upto', label: 'Valid Upto', render: (tm) => <span className="font-mono">{tm.valid_upto || '—'}</span> },
  { key: 'publication_date', label: 'Publication Date', render: (tm) => <span className="font-mono">{tm.publication_date || '—'}</span> },
  { key: 'class_description', label: 'Class Detail', render: (tm) => <span className="max-w-65 block truncate" title={tm.class_description || ''}>{tm.class_description || '—'}</span> },
  { key: 'category', label: 'Category', render: (tm) => tm.category || '—' },
  { key: 'application_filed_as', label: 'Filing Mode', render: (tm) => tm.application_filed_as || '—' },
  { key: 'office', label: 'Office', render: (tm) => tm.office || '—' },
  { key: 'country', label: 'Country', render: (tm) => tm.country || '—' },
  { key: 'certificate_detail', label: 'Certificate', render: (tm) => <span className="max-w-50 block truncate" title={tm.certificate_detail || ''}>{tm.certificate_detail || '—'}</span> },
];

const DEFAULT_COLUMNS = ['word_mark', 'class_number', 'application_date', 'portal_status', 'proprietor_name'];

// yyyy-mm-dd (native <input type=date>) <-> dd/mm/yyyy (what the backend
// stores application_date as) — kept as two small helpers rather than a
// library since it's only ever this one fixed format on each side.
function toInputDate(ddmmyyyy: string): string {
  const parts = ddmmyyyy.split('/');
  if (parts.length !== 3) return '';
  const [d, m, y] = parts;
  return `${y}-${m}-${d}`;
}
function fromInputDate(yyyymmdd: string): string {
  const parts = yyyymmdd.split('-');
  if (parts.length !== 3) return '';
  const [y, m, d] = parts;
  return `${d}/${m}/${y}`;
}

export default function Trademarks({ title = 'Portfolio Trademarks', lockedStatus, kind = 'portfolio' }: { title?: string; lockedStatus?: string; kind?: 'portfolio' | 'opposition_out' } = {}) {
  const [data, setData] = useState<any>(null);
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const [q, setQ] = useState(searchParams.get('q') || '');
  const [classFilter, setClassFilter] = useState(searchParams.get('class') || '');
  const [statusFilter, setStatusFilter] = useState(lockedStatus || searchParams.get('status') || '');
  const [stateFilter, setStateFilter] = useState(searchParams.get('state') || '');
  const [dateFrom, setDateFrom] = useState(searchParams.get('date_from') || '');
  const [dateTo, setDateTo] = useState(searchParams.get('date_to') || '');
  const [page, setPage] = useState(1);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState('');
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState('');
  const [reloadKey, setReloadKey] = useState(0);
  const filtersRef = useRef<HTMLDivElement>(null);
  const columnsRef = useRef<HTMLDivElement>(null);
  const rowsPerPage = 100;

  // Column choice/order — loaded from the server on mount (falls back to a
  // sensible default set for a brand-new user who's never customized it),
  // saved back to the server on every change so it persists across
  // sessions/devices. App Number is never part of this list; it's always
  // rendered first, outside the picker entirely.
  const [columns, setColumns] = useState<string[]>(DEFAULT_COLUMNS);
  const [columnsOpen, setColumnsOpen] = useState(false);
  const [columnsLoaded, setColumnsLoaded] = useState(false);

  useEffect(() => {
    fetch(`${API_BASE}/api/me/column-prefs`)
      .then((res) => res.json())
      .then((json) => {
        if (json.columns && json.columns.length > 0) {
          setColumns(json.columns.filter((c: string) => COLUMN_DEFS.some((d) => d.key === c)));
        }
      })
      .catch(() => {})
      .finally(() => setColumnsLoaded(true));
  }, []);

  // Skip the very first save — it would just re-POST whatever GET just
  // returned (or the untouched default before GET resolves), which is
  // never a real user change worth persisting.
  useEffect(() => {
    if (!columnsLoaded) return;
    fetch(`${API_BASE}/api/me/column-prefs`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ columns }),
    }).catch(() => {});
  }, [columns, columnsLoaded]);

  const toggleColumn = (key: string) => {
    setColumns((current) =>
      current.includes(key) ? current.filter((c) => c !== key) : [...current, key]
    );
  };

  const moveColumn = (key: string, direction: -1 | 1) => {
    setColumns((current) => {
      const idx = current.indexOf(key);
      const swapWith = idx + direction;
      if (idx === -1 || swapWith < 0 || swapWith >= current.length) return current;
      const next = [...current];
      [next[idx], next[swapWith]] = [next[swapWith], next[idx]];
      return next;
    });
  };

  useEffect(() => {
    function onClickOutsideColumns(e: MouseEvent) {
      if (columnsRef.current && !columnsRef.current.contains(e.target as Node)) {
        setColumnsOpen(false);
      }
    }
    if (columnsOpen) document.addEventListener('mousedown', onClickOutsideColumns);
    return () => document.removeEventListener('mousedown', onClickOutsideColumns);
  }, [columnsOpen]);

  useEffect(() => {
    const params = new URLSearchParams();
    params.set('page', String(page));
    params.set('limit', String(rowsPerPage));
    if (q) params.set('q', q);
    if (classFilter) params.set('class', classFilter);
    if (statusFilter) params.set('status', statusFilter);
    if (stateFilter) params.set('state', stateFilter);
    if (dateFrom) params.set('date_from', dateFrom);
    if (dateTo) params.set('date_to', dateTo);
    if (kind !== 'portfolio') params.set('kind', kind);
    fetch(`${API_BASE}/api/trademarks?${params.toString()}`)
      .then((res) => res.json())
      .then((json) => setData(json))
      .catch((err) => console.error(err));
  }, [page, q, classFilter, statusFilter, stateFilter, dateFrom, dateTo, reloadKey, kind]);

  // Selection is limited to the visible page, so a bulk action cannot
  // silently carry records across a filter or page change.
  useEffect(() => {
    setSelectedIds(new Set());
    setDeleteError('');
  }, [page, q, classFilter, statusFilter, stateFilter, dateFrom, dateTo]);

  useEffect(() => {
    function onClickOutside(e: MouseEvent) {
      if (filtersRef.current && !filtersRef.current.contains(e.target as Node)) {
        setFiltersOpen(false);
      }
    }
    if (filtersOpen) document.addEventListener('mousedown', onClickOutside);
    return () => document.removeEventListener('mousedown', onClickOutside);
  }, [filtersOpen]);

  const activeFilterCount = [classFilter, !lockedStatus && statusFilter, stateFilter, dateFrom, dateTo].filter(Boolean).length;

  const clearFilters = () => {
    setClassFilter('');
    setStatusFilter(lockedStatus || '');
    setStateFilter('');
    setDateFrom('');
    setDateTo('');
    setPage(1);
  };

  if (!data) return <div className="loading"><Ring state="loading" size={28} /><span>Loading portfolio…</span></div>;

  const totalPages = data.total_pages || Math.max(1, Math.ceil(data.total_filtered / rowsPerPage));
  const visibleIds = data.trademarks.map((tm: any) => tm.id as number);
  const allVisibleSelected = visibleIds.length > 0 && visibleIds.every((id: number) => selectedIds.has(id));

  const toggleSelected = (id: number) => {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleAllVisible = () => setSelectedIds(allVisibleSelected ? new Set() : new Set(visibleIds));

  const deleteTrademarks = async (ids: number[]) => {
    if (!ids.length || deleting) return;
    const noun = ids.length === 1 ? 'trademark' : 'trademarks';
    if (!window.confirm(`Delete ${ids.length} selected ${noun} from the portfolio? This cannot be undone.`)) return;

    setDeleting(true);
    setDeleteError('');
    try {
      const response = await fetch(`${API_BASE}/api/trademarks/delete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids }),
      });
      const json = await response.json();
      if (!response.ok || !json.ok) throw new Error(json.error || 'Could not delete the selected trademarks.');
      setSelectedIds(new Set());
      if (data.trademarks.length === ids.length && page > 1) setPage((p) => p - 1);
      else setReloadKey((key) => key + 1);
    } catch (error) {
      setDeleteError(error instanceof Error ? error.message : 'Could not delete the selected trademarks.');
    } finally {
      setDeleting(false);
    }
  };

  const exportTrademarks = async (ids: number[]) => {
    if (!ids.length || exporting) return;
    setExporting(true);
    setExportError('');
    try {
      const response = await fetch(`${API_BASE}/api/trademarks/export`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids }),
      });
      if (!response.ok) {
        const json = await response.json().catch(() => null);
        throw new Error(json?.error || 'Could not export the selected trademarks.');
      }
      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'trademarks-export.csv';
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(url);
    } catch (error) {
      setExportError(error instanceof Error ? error.message : 'Could not export the selected trademarks.');
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className="flex h-full w-full overflow-hidden relative">
      <div className="flex-1 min-w-0 flex flex-col overflow-hidden min-h-0">
        <div className="flex items-center justify-between gap-3 px-5 py-3 border-b border-border bg-panel flex-wrap">
          <div className="flex items-center gap-2.5 flex-1 min-w-[320px]">
            <div className="font-display font-semibold text-[15px] whitespace-nowrap">{title}</div>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                setPage(1);
                setSearchParams(q ? { q } : {});
              }}
              className="flex items-center border border-border-strong rounded-md overflow-hidden flex-1 max-w-[420px]"
            >
              <span className="flex items-center px-2.5 text-muted-foreground"><Search size={13} /></span>
              <input
                type="text"
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Search mark, app number or proprietor…"
                className="border-none flex-1 text-[13px] py-1.5 pr-2.5 outline-none"
              />
            </form>
          </div>
          <div className="flex items-center gap-2">
            {selectedIds.size > 0 && (
              <>
                <button
                  type="button"
                  className="btn"
                  onClick={() => exportTrademarks([...selectedIds])}
                  disabled={exporting}
                >
                  <Download size={13} /> {exporting ? 'Exporting…' : `Export (${selectedIds.size})`}
                </button>
                <button
                  type="button"
                  className="btn border-status-bad text-status-bad hover:bg-status-bad-bg"
                  onClick={() => deleteTrademarks([...selectedIds])}
                  disabled={deleting}
                >
                  <Trash2 size={13} /> {deleting ? 'Deleting…' : `Delete selected (${selectedIds.size})`}
                </button>
              </>
            )}
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
                    <span className="eyebrow text-muted-foreground!">Class</span>
                    <select
                      value={classFilter}
                      onChange={(e) => { setClassFilter(e.target.value); setPage(1); }}
                      className="border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary"
                    >
                      <option value="">All classes</option>
                      {data.available_classes?.map((c: string) => (
                        <option key={c} value={c}>Class {c}</option>
                      ))}
                    </select>
                  </label>

                  {!lockedStatus && (
                    <label className="flex flex-col gap-1.5">
                      <span className="eyebrow text-muted-foreground!">Status</span>
                      <select
                        value={statusFilter}
                        onChange={(e) => { setStatusFilter(e.target.value); setPage(1); }}
                        className="border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary"
                      >
                        <option value="">All statuses</option>
                        {data.available_statuses?.map((s: string) => (
                          <option key={s} value={s}>{s}</option>
                        ))}
                      </select>
                    </label>
                  )}

                  <div className="flex flex-col gap-1.5">
                    <span className="eyebrow text-muted-foreground!">Application date</span>
                    <div className="flex items-center gap-2">
                      <input
                        type="date"
                        value={toInputDate(dateFrom)}
                        onChange={(e) => { setDateFrom(fromInputDate(e.target.value)); setPage(1); }}
                        className="border border-border-strong rounded-md px-2 py-1.5 text-[12.5px] outline-none focus:border-primary flex-1 min-w-0"
                      />
                      <span className="text-muted-foreground text-[12px]">to</span>
                      <input
                        type="date"
                        value={toInputDate(dateTo)}
                        onChange={(e) => { setDateTo(fromInputDate(e.target.value)); setPage(1); }}
                        className="border border-border-strong rounded-md px-2 py-1.5 text-[12.5px] outline-none focus:border-primary flex-1 min-w-0"
                      />
                    </div>
                  </div>
                </div>
              )}
            </div>
            <div className="relative" ref={columnsRef}>
              <button
                type="button"
                className="btn"
                onClick={() => setColumnsOpen((v) => !v)}
              >
                <Columns3 size={12} /> Columns
              </button>
              {columnsOpen && (
                <div className="absolute right-0 top-[calc(100%+6px)] z-20 w-70 bg-panel border border-border rounded-lg shadow-lg p-4 flex flex-col gap-1">
                  <div className="flex items-center justify-between mb-1.5">
                    <span className="text-[13px] font-semibold">Columns</span>
                    <span className="text-[11.5px] text-muted-foreground">App. Num is always shown</span>
                  </div>
                  <div className="max-h-80 overflow-y-auto flex flex-col gap-0.5">
                    {[...columns.filter((k) => COLUMN_DEFS.some((d) => d.key === k)), ...COLUMN_DEFS.map((d) => d.key).filter((k) => !columns.includes(k))].map((key) => {
                      const def = COLUMN_DEFS.find((d) => d.key === key)!;
                      const checked = columns.includes(key);
                      const idx = columns.indexOf(key);
                      return (
                        <div key={key} className="flex items-center gap-2 px-1.5 py-1 rounded-md hover:bg-muted">
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={() => toggleColumn(key)}
                            id={`col-${key}`}
                          />
                          <label htmlFor={`col-${key}`} className="text-[13px] flex-1 cursor-pointer select-none">{def.label}</label>
                          {checked && (
                            <div className="flex items-center gap-0.5">
                              <button type="button" className="w-5.5 h-5.5 flex items-center justify-center text-muted-foreground hover:text-foreground disabled:opacity-30" disabled={idx === 0} onClick={() => moveColumn(key, -1)} aria-label={`Move ${def.label} up`}>
                                <ChevronUp size={13} />
                              </button>
                              <button type="button" className="w-5.5 h-5.5 flex items-center justify-center text-muted-foreground hover:text-foreground disabled:opacity-30" disabled={idx === columns.length - 1} onClick={() => moveColumn(key, 1)} aria-label={`Move ${def.label} down`}>
                                <ChevronDown size={13} />
                              </button>
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}
            </div>
            <a className="btn btn-accent" href="/add">+ Add applications</a>
          </div>
        </div>

        {deleteError && (
          <div className="px-5 py-2 text-[12.5px] bg-status-bad-bg text-status-bad border-b border-border">{deleteError}</div>
        )}
        {exportError && (
          <div className="px-5 py-2 text-[12.5px] bg-status-bad-bg text-status-bad border-b border-border">{exportError}</div>
        )}

        <div className="flex-1 overflow-auto bg-panel">
          <table className="w-full min-w-[820px] border-separate border-spacing-0">
            <thead>
              <tr>
                <th
                  className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 left-0 bg-panel z-20 w-6.5"
                  style={{ borderBottom: '1px dashed var(--color-border)', borderRight: '1px dashed var(--color-border)' }}
                >
                  <input type="checkbox" checked={allVisibleSelected} onChange={toggleAllVisible} aria-label="Select all trademarks on this page" />
                </th>
                <th
                  className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 left-6.5 bg-panel z-20 whitespace-nowrap"
                  style={{ borderBottom: '1px dashed var(--color-border)', borderRight: '1px dashed var(--color-border)' }}
                >
                  App. Num
                </th>
                {columns.map((key, i) => {
                  const def = COLUMN_DEFS.find((d) => d.key === key);
                  if (!def) return null;
                  const isLast = i === columns.length - 1;
                  return (
                    <th
                      key={key}
                      className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel z-10 whitespace-nowrap"
                      style={{ borderBottom: '1px dashed var(--color-border)', borderRight: isLast ? 'none' : '1px dashed var(--color-border)' }}
                    >
                      {def.label}
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {data.trademarks.map((tm: any) => (
                <tr
                  key={tm.id}
                  onClick={() => navigate(`/trademark/${tm.id}`, { state: lockedStatus ? { from: '/oppositions', fromLabel: title } : undefined })}
                  className="cursor-pointer hover:bg-muted"
                >
                  <td
                    className="px-2.5 py-1.5 align-middle text-[13px] sticky left-0 bg-panel z-10"
                    style={{ borderBottom: '1px dashed var(--color-border)', borderRight: '1px dashed var(--color-border)' }}
                    onClick={(e) => e.stopPropagation()}
                  >
                    <input type="checkbox" checked={selectedIds.has(tm.id)} onChange={() => toggleSelected(tm.id)} aria-label={`Select ${tm.app_no}`} />
                  </td>
                  <td
                    className="px-2.5 py-1.5 align-middle text-[13px] sticky left-6.5 bg-panel z-10 whitespace-nowrap"
                    style={{ borderBottom: '1px dashed var(--color-border)', borderRight: '1px dashed var(--color-border)' }}
                  >
                    <span className="app-no-link">{tm.app_no}</span>
                  </td>
                  {columns.map((key, i) => {
                    const def = COLUMN_DEFS.find((d) => d.key === key);
                    if (!def) return null;
                    const isLast = i === columns.length - 1;
                    return (
                      <td
                        key={key}
                        className="px-2.5 py-1.5 align-middle text-[13px]"
                        style={{ borderBottom: '1px dashed var(--color-border)', borderRight: isLast ? 'none' : '1px dashed var(--color-border)' }}
                      >
                        {def.render(tm)}
                      </td>
                    );
                  })}
                </tr>
              ))}
              {data.trademarks.length === 0 && (
                <tr><td colSpan={2 + columns.length} className="text-center py-10 text-muted-foreground">No trademarks found.</td></tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="flex justify-between items-center px-5 py-2.5 border-t border-border text-[12.5px] text-muted-foreground bg-panel flex-wrap gap-2">
          <span>Showing {data.trademarks.length} of {data.total_filtered} row(s){data.total_filtered !== data.total ? ` · ${data.total} total` : ''}.</span>
          <div className="flex items-center gap-3">
            <span>Rows per page: {rowsPerPage}</span>
            <span>Page {page} of {totalPages}</span>
            <button className="w-6 h-6 border border-border-strong rounded-md flex items-center justify-center disabled:opacity-40" disabled={page <= 1} onClick={() => setPage(page - 1)}>&lsaquo;</button>
            <button className="w-6 h-6 border border-border-strong rounded-md flex items-center justify-center disabled:opacity-40" disabled={page >= totalPages} onClick={() => setPage(page + 1)}>&rsaquo;</button>
          </div>
        </div>
      </div>
    </div>
  );
}
