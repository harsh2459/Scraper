import { useState, useMemo } from 'react';
import { Search } from 'lucide-react';
import { SCRAPER_API_BASE } from '../lib/api';
import { statusClass } from '../lib/status';
import { useToast } from '../components/tm/Toast';
import Pagination from '../components/Pagination';
import MarkThumb from '../components/MarkThumb';
import { wmOptionLabel } from '../lib/tmSearch';

type JobStatus = 'idle' | 'running' | 'done' | 'error';
type WordmarkOption = 'start_with' | 'contains' | 'match_with';

interface WordmarkResult {
  app_no: string;
  class_number: string;
  application_date: string;
  trade_mark: string;
  proprietor_name: string;
  use_statement: string | null;
  valid_upto: string;
  status: string;
  logo_data_uri: string | null;
  class_description: string | null;
  row_order: number;
}

const TM_CLASSES = Array.from({ length: 45 }, (_, i) => i + 1);
const WM_RESULTS_PAGE_SIZE = 50;

export default function WordMarkSearch() {
  const [wmText, setWmText] = useState('');
  const [wmOption, setWmOption] = useState<WordmarkOption>('contains');
  const [wmSelectedClasses, setWmSelectedClasses] = useState<Set<number>>(new Set());
  const [wmClassPickerOpen, setWmClassPickerOpen] = useState(false);
  const [wmAllClassesProgress, setWmAllClassesProgress] = useState<{ current: number; total: number } | null>(null);
  const [wmStatus, setWmStatus] = useState<JobStatus>('idle');
  const [wmMessage, setWmMessage] = useState('');
  const [wmResults, setWmResults] = useState<WordmarkResult[]>([]);
  const [wmSearchId, setWmSearchId] = useState<number | null>(null);
  const [wmExportState, setWmExportState] = useState<Record<number, 'idle' | 'preparing' | 'ready' | 'error'>>({});
  const [wmExportJobId, setWmExportJobId] = useState<Record<number, string>>({});
  const [wmResultsPage, setWmResultsPage] = useState(1);
  const { showToast } = useToast();

  // Only meaningful once results span more than one class ("All Classes")
  // or more than one status — lets you narrow a big result set instead of
  // scrolling through everything. Empty set = no filter (show everything).
  const [wmClassFilter, setWmClassFilter] = useState<Set<number>>(new Set());
  const [wmStatusFilter, setWmStatusFilter] = useState<Set<string>>(new Set());

  const wmResultsFiltered = useMemo(
    () => wmResults.filter((r) =>
      (wmClassFilter.size === 0 || wmClassFilter.has(Number(r.class_number))) &&
      (wmStatusFilter.size === 0 || wmStatusFilter.has(r.status))
    ),
    [wmResults, wmClassFilter, wmStatusFilter],
  );

  const wmResultsTotalPages = Math.max(1, Math.ceil(wmResultsFiltered.length / WM_RESULTS_PAGE_SIZE));
  const wmResultsPageRows = useMemo(
    () => wmResultsFiltered.slice((wmResultsPage - 1) * WM_RESULTS_PAGE_SIZE, wmResultsPage * WM_RESULTS_PAGE_SIZE),
    [wmResultsFiltered, wmResultsPage],
  );

  // Which classes/statuses actually appear in the current result set, with
  // counts — drives the filter checkboxes so you only see options worth
  // filtering by, not every possibility regardless of what was found.
  const wmClassCounts = useMemo(() => {
    const counts = new Map<number, number>();
    for (const r of wmResults) {
      const c = Number(r.class_number);
      if (!Number.isNaN(c)) counts.set(c, (counts.get(c) || 0) + 1);
    }
    return counts;
  }, [wmResults]);

  const wmStatusCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const r of wmResults) {
      if (r.status) counts.set(r.status, (counts.get(r.status) || 0) + 1);
    }
    return counts;
  }, [wmResults]);

  const toggleWmClassFilter = (c: number) => {
    setWmClassFilter((prev) => {
      const next = new Set(prev);
      if (next.has(c)) next.delete(c); else next.add(c);
      return next;
    });
    setWmResultsPage(1);
  };

  const toggleWmStatusFilter = (s: string) => {
    setWmStatusFilter((prev) => {
      const next = new Set(prev);
      if (next.has(s)) next.delete(s); else next.add(s);
      return next;
    });
    setWmResultsPage(1);
  };

  const startWordmarkSearch = async (e: React.FormEvent) => {
    e.preventDefault();
    const text = wmText.trim();
    if (!text || wmSelectedClasses.size === 0) return;

    // Exactly one class selected -> the plain, already-fast single-class
    // endpoint. Two or more -> the same per-class loop "All Classes" uses,
    // just with whichever classes were actually picked instead of 1-45.
    if (wmSelectedClasses.size === 1) {
      return startSingleClassSearch(text, [...wmSelectedClasses][0]);
    }
    return startMultiClassSearch(text, [...wmSelectedClasses]);
  };

  const startSingleClassSearch = async (text: string, classCode: number) => {
    setWmStatus('running');
    setWmMessage('');
    setWmResults([]);
    setWmSearchId(null);
    setWmResultsPage(1);
    setWmAllClassesProgress(null);
    setWmClassFilter(new Set());
    setWmStatusFilter(new Set());

    try {
      const res = await fetch(`${SCRAPER_API_BASE}/api/scrape-wordmark-class`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, option: wmOption, class_code: classCode }),
      });
      const json = await res.json();
      if (!res.ok || json.error) {
        setWmStatus('error');
        setWmMessage(json.error || `HTTP ${res.status}`);
        return;
      }
      setWmSearchId(json.search_id);
      pollWordmarkJob(json.search_id);
    } catch {
      setWmStatus('error');
      setWmMessage('Could not reach the search service.');
    }
  };

  const startMultiClassSearch = async (text: string, classCodes: number[]) => {
    setWmStatus('running');
    setWmMessage('');
    setWmResults([]);
    setWmSearchId(null);
    setWmResultsPage(1);
    setWmAllClassesProgress({ current: 0, total: classCodes.length });
    setWmClassFilter(new Set());
    setWmStatusFilter(new Set());

    try {
      const res = await fetch(`${SCRAPER_API_BASE}/api/scrape-wordmark-allclasses`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, option: wmOption, class_codes: classCodes }),
      });
      const json = await res.json();
      if (!res.ok || json.error) {
        setWmStatus('error');
        setWmMessage(json.error || `HTTP ${res.status}`);
        return;
      }
      pollAllClassesJob(json.job_id);
    } catch {
      setWmStatus('error');
      setWmMessage('Could not reach the search service.');
    }
  };

  const pollAllClassesJob = (jobId: string) => {
    const lastRowOrderBySearch: Record<number, number> = {};

    const interval = window.setInterval(async () => {
      try {
        const res = await fetch(`${SCRAPER_API_BASE}/api/scrape-wordmark-allclasses/${jobId}`);
        const json = await res.json();
        if (json.status === 'unknown') {
          window.clearInterval(interval);
          setWmStatus('error');
          setWmMessage('Search job was lost.');
          return;
        }
        setWmAllClassesProgress({ current: json.current_class || 0, total: json.total_classes || 45 });

        const searchIds: number[] = json.search_ids || [];
        for (const sid of searchIds) {
          const after = lastRowOrderBySearch[sid] ?? -1;
          const classRes = await fetch(`${SCRAPER_API_BASE}/api/scrape-wordmark-class/${sid}?after=${after}`);
          const classJson = await classRes.json();
          const newRows: WordmarkResult[] = classJson.results || [];
          if (newRows.length > 0) {
            lastRowOrderBySearch[sid] = newRows[newRows.length - 1].row_order;
            setWmResults((prev) => [...prev, ...newRows]);
          }
        }

        if (json.status === 'done') {
          window.clearInterval(interval);
          setWmStatus('done');
        }
      } catch {
        // transient — keep polling
      }
    }, 2000);
  };

  const pollWordmarkJob = (searchId: number) => {
    let lastRowOrder = -1;
    const interval = window.setInterval(async () => {
      try {
        const res = await fetch(`${SCRAPER_API_BASE}/api/scrape-wordmark-class/${searchId}?after=${lastRowOrder}`);
        const json = await res.json();
        const newRows: WordmarkResult[] = json.results || [];
        if (newRows.length > 0) {
          lastRowOrder = newRows[newRows.length - 1].row_order;
          setWmResults((prev) => [...prev, ...newRows]);
        }
        if (json.status === 'done') {
          window.clearInterval(interval);
          setWmStatus('done');
        } else if (json.status === 'unknown') {
          // The search row is gone — meaning it finished with 0 results
          // and was auto-removed. Normal "nothing found", not a failure.
          window.clearInterval(interval);
          setWmResults([]);
          setWmStatus('done');
        } else if (json.status === 'error') {
          window.clearInterval(interval);
          setWmStatus('error');
          setWmMessage(json.message || 'Search failed.');
        }
      } catch {
        // transient — keep polling
      }
    }, 1500);
  };

  const exportWmPdf = async (searchId: number) => {
    setWmExportState((s) => ({ ...s, [searchId]: 'preparing' }));
    try {
      const res = await fetch(`${SCRAPER_API_BASE}/api/wordmark-search/${searchId}/export-pdf`, { method: 'POST' });
      const json = await res.json();
      if (!res.ok || json.error) {
        setWmExportState((s) => ({ ...s, [searchId]: 'error' }));
        showToast('error', json.error || 'Could not start PDF export.');
        return;
      }
      const pdfJobId = json.pdf_job_id;
      setWmExportJobId((s) => ({ ...s, [searchId]: pdfJobId }));
      const interval = window.setInterval(async () => {
        try {
          const statusRes = await fetch(`${SCRAPER_API_BASE}/api/wordmark-search/export-pdf/${pdfJobId}`);
          const statusJson = await statusRes.json();
          if (statusJson.status === 'done') {
            window.clearInterval(interval);
            setWmExportState((s) => ({ ...s, [searchId]: 'ready' }));
            showToast('success', `PDF ready for search #${searchId} — click Download.`);
          } else if (statusJson.status === 'error' || statusJson.status === 'unknown') {
            window.clearInterval(interval);
            setWmExportState((s) => ({ ...s, [searchId]: 'error' }));
            showToast('error', statusJson.error || 'PDF export failed.');
          }
        } catch {
          // transient — keep polling
        }
      }, 2000);
    } catch {
      setWmExportState((s) => ({ ...s, [searchId]: 'error' }));
      showToast('error', 'Could not reach the search service.');
    }
  };

  const downloadWmPdf = (searchId: number) => {
    const pdfJobId = wmExportJobId[searchId];
    if (!pdfJobId) return;
    window.open(`${SCRAPER_API_BASE}/api/wordmark-search/export-pdf/${pdfJobId}/download`, '_blank');
  };

  return (
    <div className="flex-1 min-h-0 overflow-y-auto px-7.5 pt-6 pb-15">
      <div className="mb-5">
        <h1 className="text-[19px] font-semibold m-0">Word Mark Search</h1>
        <p className="text-muted-foreground text-[13px] mt-0.5">
          Search the Registry by word mark text, optionally narrowed to one or more classes.
        </p>
      </div>

      <form onSubmit={startWordmarkSearch} className="max-w-190 mb-5">
        <div className="flex gap-2 mb-3">
          <div className="flex items-center border border-border-strong rounded-md overflow-hidden flex-1">
            <span className="flex items-center px-2.5 text-muted-foreground"><Search size={13} /></span>
            <input
              type="text"
              value={wmText}
              onChange={(e) => setWmText(e.target.value.toUpperCase())}
              placeholder="e.g. TATA"
              className="border-none flex-1 text-[13px] py-2 pr-2.5 outline-none font-mono uppercase"
            />
          </div>
          <div className="relative">
            <button
              type="button"
              onClick={() => setWmClassPickerOpen((v) => !v)}
              className="border border-border-strong rounded-md text-[13px] px-2.5 py-2 bg-panel min-w-35 text-left flex items-center justify-between gap-2"
            >
              <span className="truncate">
                {wmSelectedClasses.size === 0
                  ? 'Class…'
                  : wmSelectedClasses.size === 45
                  ? 'All Classes'
                  : wmSelectedClasses.size === 1
                  ? `Class ${[...wmSelectedClasses][0]}`
                  : `${wmSelectedClasses.size} classes`}
              </span>
              <span className="text-muted-foreground text-[11px]">▾</span>
            </button>
            {wmClassPickerOpen && (
              <>
                <div className="fixed inset-0 z-10" onClick={() => setWmClassPickerOpen(false)} />
                <div className="absolute z-20 mt-1 w-65 border border-border-strong rounded-md bg-panel shadow-lg">
                  <div className="flex items-center justify-between px-2.5 py-2 border-b border-border text-[12px]">
                    <button
                      type="button"
                      className="text-primary hover:underline"
                      onClick={() => setWmSelectedClasses(new Set(TM_CLASSES))}
                    >
                      Select all (1–45)
                    </button>
                    <button
                      type="button"
                      className="text-muted-foreground hover:text-foreground"
                      onClick={() => setWmSelectedClasses(new Set())}
                    >
                      Clear
                    </button>
                  </div>
                  <div className="grid grid-cols-5 gap-1 p-2.5 max-h-55 overflow-y-auto">
                    {TM_CLASSES.map((c) => (
                      <label
                        key={c}
                        className={`flex items-center justify-center gap-1 text-[12px] rounded px-1.5 py-1 cursor-pointer border ${
                          wmSelectedClasses.has(c)
                            ? 'bg-accent text-accent-foreground border-accent'
                            : 'border-transparent hover:bg-muted'
                        }`}
                      >
                        <input
                          type="checkbox"
                          className="sr-only"
                          checked={wmSelectedClasses.has(c)}
                          onChange={() => {
                            setWmSelectedClasses((prev) => {
                              const next = new Set(prev);
                              if (next.has(c)) next.delete(c); else next.add(c);
                              return next;
                            });
                          }}
                        />
                        {c}
                      </label>
                    ))}
                  </div>
                  <div className="px-2.5 py-2 border-t border-border text-[12px]">
                    <button
                      type="button"
                      className="btn w-full"
                      onClick={() => setWmClassPickerOpen(false)}
                    >
                      Done ({wmSelectedClasses.size} selected)
                    </button>
                  </div>
                </div>
              </>
            )}
          </div>
          <button
            type="submit"
            className="btn btn-accent"
            disabled={wmStatus === 'running' || !wmText.trim() || wmSelectedClasses.size === 0}
          >
            {wmStatus === 'running' ? (
              <span className="flex items-center gap-1.5">
                <span className="w-3 h-3 border-2 border-white/60 border-t-transparent rounded-full animate-spin" />
                Searching{wmAllClassesProgress ? ` (${wmAllClassesProgress.current}/${wmAllClassesProgress.total})` : ''}…
              </span>
            ) : 'Search'}
          </button>
        </div>
        <div className="flex gap-4 text-[12.5px] items-center flex-wrap">
          {(['start_with', 'contains', 'match_with'] as WordmarkOption[]).map((opt) => (
            <label key={opt} className="flex items-center gap-1.5 cursor-pointer">
              <input type="radio" name="wmOption" checked={wmOption === opt} onChange={() => setWmOption(opt)} />
              {wmOptionLabel(opt)}
            </label>
          ))}
        </div>
      </form>

      {wmStatus === 'error' && (
        <div className="max-w-190 rounded-lg px-4 py-3 text-[12.5px] mb-4 bg-status-bad-bg text-status-bad">
          {wmMessage}
        </div>
      )}

      {(wmStatus === 'done' || (wmStatus === 'running' && wmResults.length > 0)) && (
        <div className="max-w-full">
          <div className="flex items-center justify-between mb-2.5 flex-wrap gap-2">
            <div className="text-[12.5px] text-muted-foreground">
              {(wmClassFilter.size > 0 || wmStatusFilter.size > 0) ? (
                <>{wmResultsFiltered.length} of {wmResults.length} result{wmResults.length === 1 ? '' : 's'} (filtered)</>
              ) : (
                <>{wmResults.length} result{wmResults.length === 1 ? '' : 's'}</>
              )}
              {wmStatus === 'running' && ' (still searching…)'}
            </div>
            {wmStatus === 'done' && wmSearchId != null && wmResults.length > 0 && (
              <WmExportButton
                searchId={wmSearchId}
                state={wmExportState[wmSearchId] || 'idle'}
                onExport={() => exportWmPdf(wmSearchId)}
                onDownload={() => downloadWmPdf(wmSearchId)}
              />
            )}
          </div>

          <div className="flex flex-wrap items-center gap-3 mb-3">
            {wmClassCounts.size > 1 && (
              <div className="flex flex-wrap gap-1.5 items-center">
                <span className="text-[11.5px] text-muted-foreground mr-1">Class:</span>
                {Array.from(wmClassCounts.keys()).sort((a, b) => a - b).map((c) => (
                  <button
                    key={c}
                    type="button"
                    onClick={() => toggleWmClassFilter(c)}
                    className={`px-2 py-0.5 rounded-full text-[11.5px] border transition-colors ${
                      wmClassFilter.has(c)
                        ? 'bg-accent text-accent-foreground border-accent'
                        : 'border-border-strong text-muted-foreground hover:text-foreground'
                    }`}
                  >
                    {c} <span className="opacity-70">({wmClassCounts.get(c)})</span>
                  </button>
                ))}
              </div>
            )}
            {wmStatusCounts.size > 1 && (
              <div className="flex flex-wrap gap-1.5 items-center">
                <span className="text-[11.5px] text-muted-foreground mr-1">Status:</span>
                {Array.from(wmStatusCounts.keys()).sort().map((s) => (
                  <button
                    key={s}
                    type="button"
                    onClick={() => toggleWmStatusFilter(s)}
                    className={`px-2 py-0.5 rounded-full text-[11.5px] border transition-colors ${
                      wmStatusFilter.has(s)
                        ? 'bg-accent text-accent-foreground border-accent'
                        : 'border-border-strong text-muted-foreground hover:text-foreground'
                    }`}
                  >
                    {s} <span className="opacity-70">({wmStatusCounts.get(s)})</span>
                  </button>
                ))}
              </div>
            )}
            {(wmClassFilter.size > 0 || wmStatusFilter.size > 0) && (
              <button
                type="button"
                onClick={() => { setWmClassFilter(new Set()); setWmStatusFilter(new Set()); setWmResultsPage(1); }}
                className="text-[11.5px] text-primary hover:underline"
              >
                Clear filters
              </button>
            )}
          </div>

          {wmResultsFiltered.length === 0 ? (
            <div className="border border-border rounded-md px-4 py-6 text-center text-[13px] text-muted-foreground">
              {wmResults.length === 0 ? 'No trademarks found.' : 'No results match the selected filters.'}
            </div>
          ) : (
            <div className="table-panel">
              <table className="w-full border-separate border-spacing-0">
                <thead>
                  <tr>
                    <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Application</th>
                    <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Word Mark</th>
                    <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Application Date</th>
                    <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Class</th>
                    <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Status</th>
                    <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Owner</th>
                  </tr>
                </thead>
                <tbody>
                  {(wmStatus === 'running' ? wmResultsFiltered : wmResultsPageRows).map((r) => (
                    <tr key={`${r.app_no}-${r.row_order}`} className="hover:bg-muted">
                      <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2 text-[13px]">
                        <a href={`/trademarks?q=${r.app_no}`} className="app-no-link">{r.app_no}</a>
                      </td>
                      <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2 text-[13px]">
                        <div className="flex items-center gap-2.5">
                          {r.logo_data_uri ? (
                            <img src={r.logo_data_uri} alt="" className="w-9 h-9 object-contain shrink-0 border border-border rounded" />
                          ) : (
                            <MarkThumb id={0} hasLogo={false} name={r.trade_mark} size={36} />
                          )}
                          <span className="font-medium">{r.trade_mark}</span>
                        </div>
                      </td>
                      <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2 text-[12.5px] text-muted-foreground font-mono">{r.application_date}</td>
                      <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2 text-[13px] text-muted-foreground">{r.class_number}</td>
                      <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2 text-[13px]">
                        <span className={`pill ${statusClass(r.status)}`}><span className="dot" />{r.status || '—'}</span>
                      </td>
                      <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2 text-[12.5px] text-muted-foreground max-w-70 truncate">{r.proprietor_name}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {wmStatus === 'done' && (
                <Pagination
                  page={wmResultsPage}
                  totalPages={wmResultsTotalPages}
                  totalRows={wmResultsFiltered.length}
                  pageRows={WM_RESULTS_PAGE_SIZE}
                  rowLabel="results"
                  onPrev={() => setWmResultsPage((p) => Math.max(1, p - 1))}
                  onNext={() => setWmResultsPage((p) => Math.min(wmResultsTotalPages, p + 1))}
                />
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function WmExportButton({
  searchId, state, onExport, onDownload,
}: {
  searchId: number;
  state: 'idle' | 'preparing' | 'ready' | 'error';
  onExport: () => void;
  onDownload: () => void;
}) {
  if (state === 'ready') {
    return (
      <button type="button" onClick={onDownload} title={`Download PDF for search #${searchId}`} className="btn btn-accent text-[11.5px] px-2.5 py-1 shrink-0">
        Download PDF
      </button>
    );
  }
  if (state === 'preparing') {
    return (
      <button type="button" disabled title={`Preparing PDF for search #${searchId}`} className="btn text-[11.5px] px-2.5 py-1 shrink-0 opacity-60">
        Preparing…
      </button>
    );
  }
  return (
    <button type="button" onClick={onExport} title={`${state === 'error' ? 'Retry PDF export' : 'Export PDF'} for search #${searchId}`} className="btn text-[11.5px] px-2.5 py-1 shrink-0">
      {state === 'error' ? 'Retry Export' : 'Export PDF'}
    </button>
  );
}
