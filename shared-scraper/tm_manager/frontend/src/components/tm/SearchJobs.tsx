import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { Loader2, ChevronDown, ChevronUp } from 'lucide-react';
import { SCRAPER_API_BASE } from '../../lib/api';
import { useToast } from './Toast';

interface SearchJob {
  id: string;      // scraper job_id
  appNo: string;
  step: string;
}

interface SearchJobsContextValue {
  activeJobs: SearchJob[];
  startJob: (jobId: string, appNo: string, onDone: (searchId: number | null, message: string) => void) => void;
}

const SearchJobsContext = createContext<SearchJobsContextValue | null>(null);

const POLL_INTERVAL_MS = 3000;

// Tracks TM Search jobs above the router so a poll started on the TM Search
// page keeps running (and the sidebar badge + completion toast keep firing)
// even after navigating to a different page — a search used to be tied to
// TmSearch's own component state, so leaving the page silently dropped the
// poll and you'd never find out it finished.
export function SearchJobsProvider({ children }: { children: React.ReactNode }) {
  const [activeJobs, setActiveJobs] = useState<SearchJob[]>([]);
  const callbacksRef = useRef<Map<string, (searchId: number | null, message: string) => void>>(new Map());
  const { showToast } = useToast();

  const startJob = useCallback((jobId: string, appNo: string, onDone: (searchId: number | null, message: string) => void) => {
    callbacksRef.current.set(jobId, onDone);
    setActiveJobs((prev) => [...prev, { id: jobId, appNo, step: 'Starting…' }]);
  }, []);

  useEffect(() => {
    if (activeJobs.length === 0) return;
    const interval = window.setInterval(async () => {
      for (const job of activeJobs) {
        try {
          const res = await fetch(`${SCRAPER_API_BASE}/api/scrape-status/${job.id}`);
          const json = await res.json();
          if (json.status === 'done' || json.status === 'error' || json.status === 'unknown') {
            const cb = callbacksRef.current.get(job.id);
            callbacksRef.current.delete(job.id);
            setActiveJobs((prev) => prev.filter((j) => j.id !== job.id));
            if (json.status === 'done' && json.search_id) {
              showToast('success', `Search complete: ${job.appNo} — ${json.message || 'found'}`);
            } else {
              showToast('error', `Search failed: ${job.appNo} — ${json.message || 'no results'}`);
            }
            cb?.(json.search_id ?? null, json.message || '');
          } else if (json.step) {
            setActiveJobs((prev) => prev.map((j) => (j.id === job.id ? { ...j, step: json.step } : j)));
          }
        } catch {
          // transient — keep polling
        }
      }
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, [activeJobs, showToast]);

  return (
    <SearchJobsContext.Provider value={{ activeJobs, startJob }}>
      {children}
      <ActiveSearchesPanel jobs={activeJobs} />
    </SearchJobsContext.Provider>
  );
}

export function useSearchJobs() {
  const ctx = useContext(SearchJobsContext);
  if (!ctx) throw new Error('useSearchJobs must be used within a SearchJobsProvider');
  return ctx;
}

// Floating panel, visible from any page, showing exactly which app numbers
// are mid-search right now and their live step text — the sidebar badge
// tells you *something* is running, this tells you *what* and *how far
// along*, without having to go back to the TM Search page to check.
function ActiveSearchesPanel({ jobs }: { jobs: { id: string; appNo: string; step: string }[] }) {
  const [collapsed, setCollapsed] = useState(false);

  // Reset to expanded whenever a fresh batch of searches starts from empty —
  // otherwise a collapse from a previous run would silently hide the next
  // one's progress too.
  const prevCountRef = useRef(0);
  useEffect(() => {
    if (prevCountRef.current === 0 && jobs.length > 0) setCollapsed(false);
    prevCountRef.current = jobs.length;
  }, [jobs.length]);

  if (jobs.length === 0) return null;
  return (
    <div className="fixed bottom-5 right-92 z-100 w-70 rounded-lg border border-border bg-panel shadow-lg print:hidden">
      <button
        type="button"
        onClick={() => setCollapsed((c) => !c)}
        className="flex items-center gap-2 px-3.5 py-2.5 w-full text-left"
      >
        <Loader2 size={13} className="animate-spin text-accent" />
        <span className="text-[12px] font-semibold flex-1">
          Searching ({jobs.length})
        </span>
        {collapsed ? <ChevronUp size={14} className="text-muted-foreground" /> : <ChevronDown size={14} className="text-muted-foreground" />}
      </button>
      {!collapsed && (
        <div className="max-h-60 overflow-y-auto border-t border-border">
          {jobs.map((job) => (
            <div key={job.id} className="px-3.5 py-2.5 border-b border-border last:border-b-0">
              <div className="font-mono text-[12.5px] font-medium">{job.appNo}</div>
              <div className="text-[11.5px] text-muted-foreground mt-0.5 truncate">{job.step}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
