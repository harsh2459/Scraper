import { useState, useEffect, useMemo, useRef, useCallback } from 'react';
import Ring from '../components/tm/Ring';
import MarkThumb from '../components/MarkThumb';
import { Search, Upload, X } from 'lucide-react';
import { API_BASE } from '../lib/api';
import Pagination from '../components/Pagination';
import TrademarkOverlay from '../components/TrademarkOverlay';

const PAGE_SIZE = 100;

// The journal PDF has no extractable image for a device mark (it's drawn
// as vector content on the page), so this renders straight from the
// journal_entry_logo route — a crop of the PDF page itself, wordmark text
// included when that's all the entry actually is. Device marks can render
// tall (the crop is however much space the journal gave the artwork above
// the app-no line), so this gets real room to show at readable size rather
// than being squashed to table-row height. The mark's name is always shown
// as its own text line below the image too — a device mark's word-mark
// name (if it has one) isn't necessarily legible from the artwork alone.
function EntryThumb({ entryId, name }: { entryId: number; name: string }) {
  const [error, setError] = useState(false);
  return (
    <div className="flex flex-col gap-1">
      {!error && (
        <img
          src={`${API_BASE}/journal-entry/${entryId}/logo`}
          alt=""
          loading="lazy"
          className="max-w-[200px] max-h-[90px] w-auto h-auto object-contain object-left flex-shrink-0"
          onError={() => setError(true)}
        />
      )}
      <div className="text-[13px] font-medium">{name || '—'}</div>
    </div>
  );
}

function ScorePill({ score }: { score: number }) {
  const pct = Math.round(score * 100);
  let cls = 'bg-status-info-bg text-status-info';
  if (pct >= 85) cls = 'bg-status-bad-bg text-status-bad';
  else if (pct >= 70) cls = 'bg-status-warn-bg text-status-warn';
  else cls = 'bg-status-info-bg text-status-info';
  return <span className={`inline-flex items-center justify-center rounded-full px-2 py-0.5 text-[11.5px] font-semibold ${cls}`}>{pct}%</span>;
}

const JOB_STATUS_LABEL: Record<string, string> = {
  pending: 'Queued…',
  parsing: 'Parsing journal pages…',
  storing: 'Saving entries…',
  matching: 'Comparing against your portfolio…',
  done: 'Done',
  error: 'Failed',
};

function UploadBanner({ job, onDismiss }: { job: any; onDismiss: () => void }) {
  if (!job) return null;
  const pct = job.total_pages ? Math.round((job.pages_done / job.total_pages) * 100) : 0;
  const multiFile = job.total_files > 1;
  const journalLabel = job.journal_date ? `${job.journal_date} (Journal No. ${job.journal_no})` : `Journal No. ${job.journal_no}`;

  if (job.status === 'done') {
    return (
      <div className="mx-5 mt-3 px-4 py-2.5 rounded-md bg-status-good-bg text-status-good text-[12.5px] flex items-center justify-between">
        <span>
          {journalLabel} processed — {job.entries_found} entries, {job.matches_found} matches found.
        </span>
        <button type="button" className="font-medium hover:underline" onClick={onDismiss}>Dismiss</button>
      </div>
    );
  }
  if (job.status === 'error') {
    return (
      <div className="mx-5 mt-3 px-4 py-2.5 rounded-md bg-status-bad-bg text-status-bad text-[12.5px] flex items-center justify-between">
        <span>Failed to process {job.filename}: {job.error}</span>
        <button type="button" className="font-medium hover:underline" onClick={onDismiss}>Dismiss</button>
      </div>
    );
  }
  return (
    <div className="mx-5 mt-3 px-4 py-2.5 rounded-md bg-status-info-bg text-status-info text-[12.5px]">
      <div className="flex items-center justify-between mb-1.5">
        <span>
          {JOB_STATUS_LABEL[job.status] || job.status}
          {job.journal_no ? ` — ${journalLabel}` : ''}
          {multiFile && job.current_filename ? ` — ${job.current_filename}` : !multiFile ? ` — ${job.filename}` : ''}
        </span>
        <span className="font-mono flex items-center gap-2">
          {multiFile ? <span>file {Math.min(job.files_done + 1, job.total_files)}/{job.total_files}</span> : null}
          {job.total_pages ? <span>{job.pages_done}/{job.total_pages}</span> : null}
        </span>
      </div>
      {job.total_pages ? (
        <div className="h-1.5 rounded-full bg-white/60 overflow-hidden">
          <div className="h-full bg-status-info rounded-full transition-[width]" style={{ width: `${pct}%` }} />
        </div>
      ) : null}
    </div>
  );
}

function UploadModal({ onClose, onSubmit }: { onClose: () => void; onSubmit: (files: File[], journalNo: string, journalDate: string) => void }) {
  const [journalNo, setJournalNo] = useState('');
  const [journalDate, setJournalDate] = useState('');
  const [files, setFiles] = useState<File[]>([]);
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const addFiles = (list: FileList | File[]) => {
    const pdfs = Array.from(list).filter((f) => f.name.toLowerCase().endsWith('.pdf'));
    setFiles((prev) => {
      const existing = new Set(prev.map((f) => f.name + f.size));
      return [...prev, ...pdfs.filter((f) => !existing.has(f.name + f.size))];
    });
  };

  const removeFile = (idx: number) => setFiles((prev) => prev.filter((_, i) => i !== idx));

  const canSubmit = journalNo.trim().length > 0 && files.length > 0;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4" onClick={onClose}>
      <div className="bg-panel rounded-lg shadow-xl w-full max-w-[520px] p-5" onClick={(e) => e.stopPropagation()}>
        <div className="text-[15px] font-semibold mb-1">Upload journal</div>
        <p className="text-[12px] text-muted-foreground mb-4">
          IP India publishes each week's journal split across several class-range PDFs — add the journal number and date once, then attach every part.
        </p>

        <div className="flex gap-3 mb-4">
          <label className="flex-1 flex flex-col gap-1">
            <span className="eyebrow text-muted-foreground!">Journal No.</span>
            <input
              type="text"
              value={journalNo}
              onChange={(e) => setJournalNo(e.target.value)}
              placeholder="e.g. 2274"
              className="border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary"
            />
          </label>
          <label className="flex-1 flex flex-col gap-1">
            <span className="eyebrow text-muted-foreground!">Date of publication</span>
            <input
              type="date"
              value={journalDate}
              onChange={(e) => setJournalDate(e.target.value)}
              className="border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary"
            />
          </label>
        </div>

        <div
          className={`border-2 border-dashed rounded-lg px-4 py-6 text-center cursor-pointer transition-colors ${dragOver ? 'border-primary bg-status-info-bg' : 'border-border-strong'}`}
          onClick={() => fileInputRef.current?.click()}
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragOver(false);
            if (e.dataTransfer.files) addFiles(e.dataTransfer.files);
          }}
        >
          <Upload size={18} className="mx-auto mb-1.5 text-muted-foreground" />
          <div className="text-[13px] font-medium">Drop class-range PDFs here, or click to browse</div>
          <div className="text-[11.5px] text-muted-foreground mt-0.5">You can select multiple files at once</div>
          <input
            ref={fileInputRef}
            type="file"
            accept="application/pdf"
            multiple
            className="hidden"
            onChange={(e) => {
              if (e.target.files) addFiles(e.target.files);
              e.target.value = '';
            }}
          />
        </div>

        {files.length > 0 && (
          <div className="mt-3 flex flex-col gap-1.5 max-h-[160px] overflow-y-auto">
            {files.map((f, idx) => (
              <div key={f.name + f.size} className="flex items-center justify-between bg-muted rounded-md px-2.5 py-1.5 text-[12.5px]">
                <span className="truncate">{f.name}</span>
                <button type="button" className="text-muted-foreground hover:text-status-bad flex-shrink-0 ml-2" onClick={() => removeFile(idx)}>
                  <X size={13} />
                </button>
              </div>
            ))}
          </div>
        )}

        <div className="flex justify-end gap-2 mt-5">
          <button type="button" className="btn-ghost text-[13px] px-3 py-1.5 rounded-md" onClick={onClose}>Cancel</button>
          <button
            type="button"
            className="btn btn-primary disabled:opacity-50"
            disabled={!canSubmit}
            onClick={() => onSubmit(files, journalNo.trim(), journalDate)}
          >
            Upload {files.length > 0 ? `${files.length} file${files.length === 1 ? '' : 's'}` : ''}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function JournalWatch() {
  const [data, setData] = useState<any>(null);
  const [switchingJournal, setSwitchingJournal] = useState(false);
  const [q, setQ] = useState('');
  const [minScore, setMinScore] = useState(70);
  const [sameClassOnly, setSameClassOnly] = useState(false);
  const [selectedJournal, setSelectedJournal] = useState<string>('');
  const [job, setJob] = useState<any>(null);
  const [uploadModalOpen, setUploadModalOpen] = useState(false);
  const [page, setPage] = useState(1);
  const [openTmId, setOpenTmId] = useState<number | null>(null);
  const pollRef = useRef<number | null>(null);
  const isFirstLoad = useRef(true);

  const loadData = useCallback((journalNo?: string, minSimilarityPct?: number) => {
    const params = new URLSearchParams();
    if (journalNo) params.set('journal_no', journalNo);
    // Sent as a 0-1 fraction (the backend's own unit) — this is what
    // actually shrinks the query/response now instead of only filtering
    // client-side after downloading everything. Falls back to the UI's own
    // default (70%) if a caller ever omits it.
    params.set('min_similarity', String((minSimilarityPct ?? 70) / 100));
    fetch(`${API_BASE}/api/journal-watch?${params.toString()}`)
      .then((res) => res.json())
      .then((json) => setData(json))
      .catch((err) => console.error(err))
      .finally(() => setSwitchingJournal(false));
  }, []);

  useEffect(() => {
    // On every switch after the first load, mark "switching" immediately so
    // the previous journal's matches never linger on screen while the new
    // ones load — without this, clicking a different journal tab kept
    // showing the OLD journal's data until the fetch resolved, since
    // setData only ever runs once the new response arrives. Also re-fetches
    // whenever minScore changes — raising the Min. similarity slider now
    // actually shrinks what's requested from the server instead of just
    // hiding rows client-side.
    if (!isFirstLoad.current) setSwitchingJournal(true);
    isFirstLoad.current = false;
    loadData(selectedJournal || undefined, minScore);
  }, [loadData, selectedJournal, minScore]);

  // Recover an in-progress job on page load/refresh so a slow parse
  // already running isn`t silently lost from view.
  useEffect(() => {
    fetch(`${API_BASE}/api/journal/jobs/latest`)
      .then((res) => res.json())
      .then((latest) => {
        if (latest && !['done', 'error'].includes(latest.status)) {
          setJob(latest);
        }
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (!job || ['done', 'error'].includes(job.status)) {
      if (pollRef.current) window.clearInterval(pollRef.current);
      return;
    }
    pollRef.current = window.setInterval(() => {
      fetch(`${API_BASE}/api/journal/jobs/${job.id}`)
        .then((res) => res.json())
        .then((updated) => {
          setJob(updated);
          if (updated.status === 'done') {
            loadData(selectedJournal || undefined, minScore);
          }
        })
        .catch(() => {});
    }, 3000);
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
  }, [job, loadData, selectedJournal, minScore]);

  const handleUpload = async (files: File[], journalNo: string, journalDate: string) => {
    const formData = new FormData();
    formData.append('journal_no', journalNo);
    if (journalDate) formData.append('journal_date', journalDate);
    files.forEach((f) => formData.append('files', f));
    try {
      const res = await fetch(`${API_BASE}/api/journal/upload`, { method: 'POST', body: formData });
      const json = await res.json();
      if (res.ok) {
        setUploadModalOpen(false);
        setJob({
          id: json.job_id,
          status: 'pending',
          filename: files.length === 1 ? files[0].name : `${files.length} files`,
          journal_no: journalNo,
          journal_date: journalDate,
          total_files: files.length,
          files_done: 0,
          pages_done: 0,
          total_pages: null,
        });
      } else {
        alert(json.error || 'Upload failed.');
      }
    } catch (err) {
      alert('Network error — could not reach the server.');
    }
  };

  const filtered = useMemo(() => {
    if (!data?.matches) return [];
    const needle = q.trim().toLowerCase();
    return data.matches.filter((m: any) => {
      // The server already only returns matches >= minScore (see loadData)
      // — this is just a rounding-safe belt-and-suspenders check, not the
      // real filter anymore, so it's cheap even on a large result.
      if (Math.round(m.similarity * 100) < minScore) return false;
      // A mark filed under a different class than the journal entry isn't
      // usually a real conflict under trademark law, so this narrows to
      // pairs sharing a class — skipped (kept out) when either side's
      // class is unknown, since there's nothing to compare there.
      if (sameClassOnly) {
        if (!m.tm_class_number || !m.class_number) return false;
        if (m.tm_class_number !== m.class_number) return false;
      }
      if (!needle) return true;
      return (
        (m.tm_word_mark || '').toLowerCase().includes(needle) ||
        (m.tm_app_no || '').toLowerCase().includes(needle) ||
        (m.entry_word_mark || '').toLowerCase().includes(needle) ||
        (m.entry_applicant_name || '').toLowerCase().includes(needle)
      );
    });
  }, [data, q, minScore, sameClassOnly]);

  // Reset to page 1 whenever any filter or journal changes
  useEffect(() => { setPage(1); }, [q, minScore, sameClassOnly, selectedJournal]);

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const pageMatches = useMemo(
    () => filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE),
    [filtered, page],
  );

  if (!data) return <div className="loading"><Ring state="loading" size={28} /><span>Loading journal watch…</span></div>;

  const uploadControls = (
    <button type="button" className="btn btn-accent" onClick={() => setUploadModalOpen(true)}>
      <Upload size={13} /> Upload journal
    </button>
  );

  if (!data.journal_no) {
    return (
      <>
        <div className="flex-1 min-h-0 overflow-y-auto px-7.5 pt-6 pb-15">
          <div className="mb-5 flex items-center justify-between">
            <h1 className="text-[19px] font-semibold m-0">Journal Watch</h1>
            {uploadControls}
          </div>
          <UploadBanner job={job} onDismiss={() => setJob(null)} />
          <div className="max-w-[480px] py-10">
            <p className="text-muted-foreground text-[13.5px] leading-relaxed m-0">
              No journal has been parsed yet. Upload a Trade Marks Journal PDF to match it against your portfolio.
            </p>
          </div>
        </div>
        {uploadModalOpen && <UploadModal onClose={() => setUploadModalOpen(false)} onSubmit={handleUpload} />}
      </>
    );
  }

  return (
    <div className="flex-1 min-h-0 flex flex-col overflow-hidden">
      {data.journals?.length > 0 && (
        <div className="flex items-center gap-2 px-5 py-2.5 border-b border-border bg-panel overflow-x-auto [&::-webkit-scrollbar]:hidden flex-shrink-0">
          {data.journals.map((j: any) => {
            const isActive = (selectedJournal || data.journal_no) === j.journal_no;
            return (
              <button
                key={j.journal_no}
                onClick={() => setSelectedJournal(j.journal_no)}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md border transition-all flex-shrink-0 ${
                  isActive
                    ? 'border-primary/40 bg-[rgba(20,71,230,0.05)] text-primary'
                    : 'border-border bg-background hover:border-border-strong hover:bg-muted text-foreground'
                }`}
              >
                <span className="text-[13px] font-medium">
                  {j.journal_date || 'Unknown'}
                </span>
                <span className={`text-[11.5px] ${isActive ? 'text-primary/70' : 'text-muted-foreground'}`}>
                  No. {j.journal_no}
                </span>
              </button>
            );
          })}
        </div>
      )}

      <div className="flex items-center justify-between gap-3 px-5 py-2.5 border-b border-border bg-panel flex-wrap flex-shrink-0">
        <div className="flex flex-col gap-0.5">
          <div className="font-display font-semibold text-[15px]">Journal Watch</div>
          <div className="flex items-baseline gap-1.5">
            <span className="text-[13px] font-medium text-foreground">
              {data.journals?.find((j: any) => j.journal_no === (selectedJournal || data.journal_no))?.journal_date || data.journals?.[0]?.journal_date}
            </span>
            <span className="text-[11.5px] text-muted-foreground">
              Journal No. {selectedJournal || data.journal_no}
            </span>
          </div>
        </div>
        <div className="flex items-center gap-3 flex-1 justify-end flex-wrap">
          <label className="flex items-center gap-2 text-[12.5px] text-muted-foreground cursor-pointer">
            <input
              type="checkbox"
              checked={sameClassOnly}
              onChange={(e) => setSameClassOnly(e.target.checked)}
              className="w-3.5 h-3.5"
            />
            Same class only
          </label>
          <label className="flex items-center gap-2 text-[12.5px] text-muted-foreground">
            Min. similarity
            <select
              value={minScore}
              onChange={(e) => setMinScore(Number(e.target.value))}
              className="border border-border-strong rounded-md px-2 py-1 text-[12.5px] outline-none"
            >
              <option value={55}>55%</option>
              <option value={70}>70%</option>
              <option value={85}>85%</option>
            </select>
          </label>
          <div className="flex items-center border border-border-strong rounded-md overflow-hidden max-w-[320px] flex-1 min-w-[200px]">
            <span className="flex items-center px-2.5 text-muted-foreground"><Search size={13} /></span>
            <input
              type="text"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search word mark, app number…"
              className="border-none flex-1 text-[13px] py-1.5 pr-2.5 outline-none"
            />
          </div>
          {uploadControls}
        </div>
      </div>

      <UploadBanner job={job} onDismiss={() => setJob(null)} />

      <div className="flex-1 min-h-0 overflow-y-auto">
        {switchingJournal ? (
          <div className="loading"><Ring state="loading" size={28} /><span>Loading journal…</span></div>
        ) : filtered.length ? (
          <table className="w-full border-separate border-spacing-0">
            <thead>
              <tr>
                <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Portfolio TM</th>
                <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-center text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Similarity</th>
                <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Published TM</th>
                <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Applicant &amp; Address</th>
              </tr>
            </thead>
            <tbody>
              {pageMatches.map((m: any, idx: number) => (
                <tr key={idx} className="hover:bg-muted align-top">
                  <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-3 text-[13px]">
                    <div className="flex items-center gap-2.5">
                      <MarkThumb id={m.tm_id} hasLogo={m.tm_has_logo} name={m.tm_word_mark || m.tm_label} />
                      <div>
                        <button type="button" onClick={() => setOpenTmId(m.tm_id)} className="app-no-link">{m.tm_app_no}</button>
                        <div className="font-medium">{m.tm_word_mark || m.tm_label || '—'}</div>
                        <div className="text-[11.5px] text-muted-foreground">Class {m.tm_class_number || '—'}</div>
                      </div>
                    </div>
                  </td>
                  <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-3 text-center"><ScorePill score={m.similarity} /></td>
                  <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-3 text-[13px]">
                    <div className="flex flex-col gap-1.5">
                      <EntryThumb entryId={m.entry_id} name={m.entry_word_mark} />
                      <div className="text-[12px] text-muted-foreground font-mono flex items-center gap-1.5">
                        {m.entry_app_no} &middot; {m.entry_app_date} &middot;
                        <span className={m.tm_class_number && m.class_number && m.tm_class_number === m.class_number ? 'text-status-bad font-semibold' : ''}>
                          Class {m.class_number || '—'}
                        </span>
                      </div>
                    </div>
                  </td>
                  <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-3 text-[12.5px] text-muted-foreground max-w-[280px]">
                    <div className="font-medium text-foreground">{m.entry_applicant_name || '—'}</div>
                    {m.entry_applicant_address && <div className="mt-0.5">{m.entry_applicant_address}</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="py-10 text-center text-muted-foreground text-[13.5px]">
            {q || minScore > 70 ? 'No matches at this filter.' : 'No similar marks found in this journal.'}
          </div>
        )}
      </div>
      {filtered.length > 0 && (
        <Pagination
          page={page}
          totalPages={totalPages}
          totalRows={filtered.length}
          pageRows={PAGE_SIZE}
          rowLabel="matches"
          onPrev={() => setPage((p) => Math.max(1, p - 1))}
          onNext={() => setPage((p) => Math.min(totalPages, p + 1))}
        />
      )}
      {uploadModalOpen && <UploadModal onClose={() => setUploadModalOpen(false)} onSubmit={handleUpload} />}
      {openTmId && <TrademarkOverlay tmId={openTmId} onClose={() => setOpenTmId(null)} />}
    </div>
  );
}
