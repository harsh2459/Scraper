import { useState, useEffect, useCallback, useMemo } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Search, History, ChevronLeft, FileText } from 'lucide-react';
import { API_BASE, SCRAPER_API_BASE } from '../lib/api';
import { statusClass } from '../lib/status';
import { useSearchJobs } from '../components/tm/SearchJobs';
import Pagination from '../components/Pagination';
import { Field } from '../components/FieldRow';

type JobStatus = 'idle' | 'running' | 'done' | 'error';

interface HistoryRow {
  search_id: number;
  app_no: string;
  trade_mark: string | null;
  status: string | null;
  error: string;
  scraped_at: string;
}

export default function AppNoSearch() {
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState<JobStatus>('idle');
  const [message, setMessage] = useState('');
  const [result, setResult] = useState<any>(null);
  const [searchedQuery, setSearchedQuery] = useState('');
  const [saveState, setSaveState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle');
  const [history, setHistory] = useState<HistoryRow[]>([]);
  const [historyPage, setHistoryPage] = useState(1);
  const [docsStatus, setDocsStatus] = useState<'none' | 'pending' | 'saving' | 'done'>('none');
  const [documents, setDocuments] = useState<{ filename: string; url: string }[]>([]);
  const { startJob } = useSearchJobs();

  const HISTORY_PAGE_SIZE = 20;

  const historyTotalPages = Math.max(1, Math.ceil(history.length / HISTORY_PAGE_SIZE));
  const historyPageRows = useMemo(
    () => history.slice((historyPage - 1) * HISTORY_PAGE_SIZE, historyPage * HISTORY_PAGE_SIZE),
    [history, historyPage],
  );

  const loadHistory = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/search-history?limit=200`);
      const json = await res.json();
      setHistory(json.history || []);
    } catch {
      // non-critical — history panel just stays empty/stale
    }
  }, []);

  useEffect(() => { loadHistory(); }, [loadHistory]);

  const backToHistory = () => {
    setStatus('idle');
    setResult(null);
    setMessage('');
    setQuery('');
    setSearchedQuery('');
    setSaveState('idle');
  };

  const viewHistoryResult = async (row: HistoryRow) => {
    setQuery(row.app_no);
    setSearchedQuery(row.app_no);
    setStatus('running');
    setMessage('');
    setResult(null);
    setSaveState('idle');
    setDocsStatus('none');
    setDocuments([]);
    try {
      const detailRes = await fetch(`${API_BASE}/api/lookup-result/${row.search_id}?search_type=appno&app_no=${encodeURIComponent(row.app_no)}`);
      const detailJson = await detailRes.json();
      if (detailJson.error) {
        setStatus('error');
        setMessage(detailJson.error);
      } else {
        setResult(detailJson.data);
        setStatus('done');
        const docs = detailJson.documents || [];
        setDocuments(docs);
        setDocsStatus(docs.length > 0 ? 'done' : 'none');
      }
    } catch {
      setStatus('error');
      setMessage('Could not load that result.');
    }
  };

  const pollDocuments = (jobId: string) => {
    const interval = window.setInterval(async () => {
      try {
        const res = await fetch(`${SCRAPER_API_BASE}/api/scrape-status/${jobId}`);
        const json = await res.json();
        setDocuments(json.documents || []);
        if (json.docs_status) setDocsStatus(json.docs_status);
        if (json.docs_status === 'done' || json.docs_status === 'none' || json.status === 'unknown') {
          window.clearInterval(interval);
        }
      } catch {
        // transient — keep polling
      }
    }, 2000);
  };

  const startSearch = (e: React.FormEvent) => {
    e.preventDefault();
    void runSearch(query);
  };

  // The sidebar's "Quick status" box opens this page with ?q=<application number>.
  const [params, setParams] = useSearchParams();
  const quickQuery = params.get('q');
  useEffect(() => {
    if (!quickQuery) return;
    setQuery(quickQuery);
    setParams({}, { replace: true });
    void runSearch(quickQuery);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [quickQuery]);

  const runSearch = async (raw: string) => {
    const q = raw.trim();
    if (!q) return;

    setStatus('running');
    setMessage('');
    setResult(null);
    setSaveState('idle');
    setDocsStatus('none');
    setDocuments([]);
    setSearchedQuery(q);

    // Plan enforcement happens here, not in the scraper — the scraper is
    // shared infrastructure with no concept of per-user plans/limits, so
    // the frontend asks tm_manager's own billing check BEFORE either the
    // cache lookup or a live scrape, and simply doesn't proceed if blocked.
    try {
      const checkRes = await fetch(`${API_BASE}/api/search-check`);
      const checkJson = await checkRes.json();
      if (!checkJson.allowed) {
        setStatus('error');
        setMessage(checkJson.reason || 'You have reached your plan\'s search limit.');
        return;
      }
    } catch {
      // tm_manager unreachable — fail open rather than blocking every
      // search just because this one check couldn't be made.
    }

    // Check what's already on file before bothering the live (occasionally
    // down/rate-limited) IP India portal at all.
    try {
      const cachedRes = await fetch(`${API_BASE}/api/lookup-cached?app_no=${encodeURIComponent(q)}`);
      const cachedJson = await cachedRes.json();
      if (cachedJson.data) {
        setResult(cachedJson.data);
        setStatus('done');
        setDocuments(cachedJson.documents || []);
        setDocsStatus((cachedJson.documents || []).length > 0 ? 'done' : 'none');
        fetch(`${API_BASE}/api/search-history`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ app_no: q, search_id: cachedJson.search_id }),
        }).catch(() => {});
        return;
      }
    } catch (err) {
      // fall through to a live search
    }

    try {
      const res = await fetch(`${SCRAPER_API_BASE}/api/scrape-appno`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ app_no: q, search_type: 'appno' }),
      });
      const json = await res.json();
      if (!res.ok || json.error) {
        setStatus('error');
        setMessage(json.error || `HTTP ${res.status}`);
        return;
      }
      // Hand the job off to the app-level tracker — it keeps polling (and
      // will toast + update the sidebar badge) even if you leave this page,
      // so a search no longer has to block you from doing anything else
      // while it runs.
      startJob(json.job_id, q, async (searchId, msg) => {
        if (searchId) {
          const detailRes = await fetch(`${API_BASE}/api/lookup-result/${searchId}?search_type=appno&app_no=${encodeURIComponent(q)}`);
          const detailJson = await detailRes.json();
          setResult(detailJson.data);
          setStatus('done');
          setDocsStatus('pending');
          setDocuments([]);
          pollDocuments(json.job_id);
          fetch(`${API_BASE}/api/search-history`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ app_no: q, search_id: searchId }),
          }).catch(() => {});
        } else {
          setStatus('error');
          setMessage(msg || 'No results found.');
        }
        loadHistory();
      });
    } catch (err) {
      setStatus('error');
      setMessage('Could not reach the scraper. Is it running?');
    }
  };

  const save = async (appNo: string) => {
    setSaveState('saving');
    try {
      const res = await fetch(`${API_BASE}/api/add`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ app_no: appNo }),
      });
      const json = await res.json();
      setSaveState(json.error ? 'error' : 'saved');
    } catch (err) {
      setSaveState('error');
    }
  };

  return (
    <div className="flex-1 min-h-0 overflow-y-auto px-7.5 pt-6 pb-15">
      <div className="mb-5">
        <h1 className="text-[19px] font-semibold m-0">Application Number Search</h1>
        <p className="text-muted-foreground text-[13px] mt-0.5">
          Look up a trademark's live status from the Registry by its application number — save it to your portfolio if you want to track it.
        </p>
      </div>

      <form onSubmit={startSearch} className="flex gap-2 max-w-[500px] mb-5">
        <div className="flex items-center border border-border-strong rounded-md overflow-hidden flex-1">
          <span className="flex items-center px-2.5 text-muted-foreground"><Search size={13} /></span>
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="e.g. 1234567"
            inputMode="numeric"
            className="border-none flex-1 text-[13px] py-2 pr-2.5 outline-none font-mono"
          />
        </div>
        <button type="submit" className="btn btn-accent" disabled={status === 'running'}>
          {status === 'running' ? (
            <span className="flex items-center gap-1.5">
              <span className="w-3 h-3 border-2 border-white/60 border-t-transparent rounded-full animate-spin" />
              Searching…
            </span>
          ) : 'Search'}
        </button>
      </form>

      {status === 'error' && (
        <div className="max-w-[500px] rounded-lg px-4 py-3 text-[12.5px] mb-4 bg-status-bad-bg text-status-bad">
          {message}
        </div>
      )}

      {/* Keep prior lookups accessible while a new live search runs or
          reports an error. A completed result has its own Back to History
          action below, so only that state replaces the list. */}
      {status !== 'done' && history.length > 0 && (
        <div className="max-w-190">
          <div className="flex items-center gap-1.5 mb-2.5">
            <History size={13} className="text-muted-foreground" />
            <span className="eyebrow text-muted-foreground">History</span>
          </div>
          <div className="border border-border rounded-md">
            <div className="divide-y divide-border">
              {historyPageRows.map((row) => (
                <button
                  key={row.search_id}
                  type="button"
                  onClick={() => viewHistoryResult(row)}
                  className="w-full flex items-center justify-between gap-3 px-4 py-2.5 text-left hover:bg-muted"
                >
                  <div className="min-w-0 flex-1 flex items-center gap-3">
                    <span className="font-mono text-[12.5px] text-muted-foreground w-21.25 shrink-0">{row.app_no}</span>
                    <span className="text-[13px] truncate">{row.trade_mark || '—'}</span>
                  </div>
                  <div className="flex items-center gap-3 shrink-0">
                    {row.error ? (
                      <span className="text-[11.5px] text-status-bad">{row.error}</span>
                    ) : (
                      <span className={`pill ${statusClass(row.status)}`}><span className="dot" />{row.status || '—'}</span>
                    )}
                    <span className="text-[11px] text-muted-foreground font-mono w-27.5 text-right">
                      {new Date(row.scraped_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
                    </span>
                  </div>
                </button>
              ))}
            </div>
            <Pagination
              page={historyPage}
              totalPages={historyTotalPages}
              totalRows={history.length}
              pageRows={HISTORY_PAGE_SIZE}
              rowLabel="searches"
              onPrev={() => setHistoryPage((p) => Math.max(1, p - 1))}
              onNext={() => setHistoryPage((p) => Math.min(historyTotalPages, p + 1))}
            />
          </div>
        </div>
      )}

      {status === 'done' && result && (
        <div className="max-w-240 flex items-start gap-5">
        <div className="flex-1 min-w-0">
          <button
            type="button"
            onClick={backToHistory}
            className="flex items-center gap-1 text-[12.5px] text-muted-foreground hover:text-foreground mb-2.5"
          >
            <ChevronLeft size={13} /> Back to History
          </button>
        <div className="border border-border rounded-md">
          <div className="flex justify-between items-start gap-4 px-5 py-4.5 border-b border-border">
            <div className="flex justify-between items-start gap-4 flex-1">
              <div>
                <h2 className="text-[18px] font-semibold m-0">{result.trade_mark || searchedQuery}</h2>
                <div className="mt-2">
                  <span className={`pill ${statusClass(result.status)}`}><span className="dot" />{result.status || '—'}</span>
                </div>
              </div>
              {result.has_logo && (
                <div className="max-w-[220px] max-h-[120px] flex-shrink-0">
                  <img
                    src={`${API_BASE}/api/lookup-logo/${searchedQuery}`}
                    alt=""
                    className="max-w-[220px] max-h-[120px] w-auto h-auto object-contain block"
                  />
                </div>
              )}
            </div>
            <button
              type="button"
              className={`btn flex-shrink-0 ${saveState === 'saved' ? 'border-status-good text-status-good' : ''}`}
              onClick={() => save(searchedQuery)}
              disabled={saveState === 'saving' || saveState === 'saved'}
            >
              {saveState === 'saving' ? 'Saving…' : saveState === 'saved' ? 'Saved ✓' : 'Save to portfolio'}
            </button>
          </div>

          {saveState === 'saved' && (
            <div className="px-5 py-2.5 text-[12.5px] bg-status-good-bg text-status-good">
              Added — <a href={`/trademarks?q=${searchedQuery}`} className="font-medium underline">view in Trademarks</a>
            </div>
          )}
          {saveState === 'error' && (
            <div className="px-5 py-2.5 text-[12.5px] bg-status-bad-bg text-status-bad">
              Could not save. It may already be in your portfolio.
            </div>
          )}

          <div className="grid grid-cols-2 gap-x-5 gap-y-3 px-5 py-4.5">
            <Field label="Application No." value={searchedQuery} mono />
            <Field label="Class" value={result.class_number || '—'} />
            <Field label="Application Date" value={result.application_date || '—'} />
            <Field label="User Detail" value={result.use_statement || 'Proposed to be used'} />
            <Field label="Type" value={result.mark_category || result.tm_type || '—'} />
            <Field label="Category" value="Trade Mark" />
            <Field label="Sub Status" value={result.sub_status || '—'} />
            <Field label="Filing Mode" value={result.application_filed_as || 'e-Filing'} />
            <Field label="State" value={result.state || '—'} />
            <Field label="Office" value={result.registry_office || '—'} />
          </div>

          {result.applicant_name && (
            <div className="px-5 py-3.5 border-t border-border">
              <h4 className="text-[13px] font-semibold m-0 mb-2">Owner</h4>
              <div className="text-[13.5px]">{result.applicant_name}</div>
              {result.applicant_address && <div className="text-[13.5px] mt-2 text-muted-foreground">{result.applicant_address}</div>}
              <div className="grid grid-cols-2 gap-x-5 gap-y-2 mt-3">
                <Field label="Legal Status" value={result.applicant_legal_status || '—'} />
                <Field label="Nature" value={result.applicant_nature || '—'} />
                <Field label="Country" value={result.applicant_country || '—'} />
                <Field label="Jurisdiction" value={result.applicant_jurisdiction || '—'} />
                <Field label="Email" value={result.applicant_email || '—'} />
                <Field label="Mobile" value={result.applicant_mobile || '—'} />
              </div>
            </div>
          )}

          {result.agent_name && (
            <div className="px-5 py-3.5 border-t border-border">
              <h4 className="text-[13px] font-semibold m-0 mb-2">Attorney</h4>
              <div className="text-[13.5px]">{result.agent_name}</div>
              {result.agent_address && <div className="text-[13.5px] mt-2 text-muted-foreground">{result.agent_address}</div>}
              <div className="grid grid-cols-2 gap-x-5 gap-y-2 mt-3">
                <Field label="Nature" value={result.agent_nature || '—'} />
                <Field label="Registration No." value={result.agent_registration_no || '—'} mono />
              </div>
            </div>
          )}

          {result.class_description && (
            <div className="px-5 py-3.5 border-t border-border">
              <h4 className="text-[13px] font-semibold m-0 mb-2">Class detail</h4>
              <div className="text-[13px] leading-relaxed">
                <b className="bg-status-good-bg text-status-good px-1.5 py-px rounded font-semibold">Class {result.class_number || '—'}</b> &mdash; {result.class_description}
              </div>
            </div>
          )}

          <div className="grid grid-cols-2 gap-x-5 gap-y-3 px-5 py-3.5 border-t border-border">
            <Field label="Fee" value={result.fee || '—'} />
            <Field label="Receipt No." value={result.receipt_no || '—'} mono />
            <Field label="Receipt Date" value={result.receipt_date || '—'} />
            <Field label="Publication Date" value={result.publication_date || '—'} />
          </div>

          {result.portal_fields && result.portal_fields.length > 0 && (
            <div className="px-5 py-3.5 border-t border-border">
              <h4 className="text-[13px] font-semibold m-0 mb-2">All Registry fields</h4>
              <div className="grid grid-cols-2 gap-x-5 gap-y-2">
                {result.portal_fields.map(([k, v]: [string, string], i: number) => (
                  <Field key={i} label={k} value={v} />
                ))}
              </div>
            </div>
          )}

        </div>
        </div>

        {/* Right-side panel — mirrors DetailPane.tsx's sidebar layout. Documents live
            here instead of at the bottom of the main column so they're visible
            without scrolling past the whole registry field list. */}
        <div className="w-70 shrink-0 border border-border rounded-md">
          <div className="px-4 py-3.5">
            <h4 className="text-[13px] font-semibold m-0 mb-2 flex items-center gap-2">
              Uploaded documents
              {(docsStatus === 'pending' || docsStatus === 'saving') && (
                <span className="text-[11.5px] font-normal text-muted-foreground flex items-center gap-1.5">
                  <span className="w-3 h-3 border-2 border-accent border-t-transparent rounded-full animate-spin" />
                  Fetching…
                </span>
              )}
            </h4>
            {documents.length === 0 ? (
              <div className="text-[12.5px] text-muted-foreground">
                {docsStatus === 'pending' || docsStatus === 'saving' ? 'Fetching documents…' : 'No documents found.'}
              </div>
            ) : (
              <div className="flex flex-col gap-1.5">
                {documents.map((d) => (
                  <a
                    key={d.filename}
                    href={`${SCRAPER_API_BASE}${d.url}`}
                    target="_blank"
                    rel="noreferrer"
                    className="flex items-center gap-2 text-[12.5px] text-primary hover:underline"
                  >
                    <FileText size={13} className="text-muted-foreground shrink-0" />
                    {d.filename}
                  </a>
                ))}
              </div>
            )}
          </div>
        </div>
        </div>
      )}
    </div>
  );
}
