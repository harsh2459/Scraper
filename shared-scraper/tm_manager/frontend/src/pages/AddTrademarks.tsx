import { useRef, useState } from 'react';
import { API_BASE } from '../lib/api';

type Kind = 'portfolio' | 'opposition_out';

export default function AddTrademarks() {
  const [kind, setKind] = useState<Kind>('portfolio');
  const [appNo, setAppNo] = useState('');
  const [bulk, setBulk] = useState('');
  const [result, setResult] = useState<any>(null);
  const [submitting, setSubmitting] = useState(false);
  const [csvFile, setCsvFile] = useState<File | null>(null);
  const [uploadingCsv, setUploadingCsv] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const selectKind = (next: Kind) => {
    setKind(next);
    setResult(null);
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setResult(null);
    try {
      const res = await fetch(`${API_BASE}/api/add`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ app_no: appNo, app_nos_bulk: bulk, kind }),
      });
      const json = await res.json();
      setResult(json);
      // Clear the form on any successful submit — even if every number
      // turned out to be already tracked, there's nothing left to fix in
      // the input, so leaving stale text there just invites a duplicate
      // resubmit.
      if (!json.error) {
        setAppNo('');
        setBulk('');
      }
    } catch (err) {
      setResult({ error: 'Network error — could not reach the server.' });
    } finally {
      setSubmitting(false);
    }
  };

  const submitCsv = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!csvFile) return;
    setUploadingCsv(true);
    setResult(null);
    try {
      const formData = new FormData();
      formData.append('file', csvFile);
      formData.append('kind', kind);
      const res = await fetch(`${API_BASE}/api/add-csv`, {
        method: 'POST',
        body: formData,
      });
      const json = await res.json();
      setResult(json);
      if (!json.error) {
        setCsvFile(null);
        if (fileInputRef.current) fileInputRef.current.value = '';
      }
    } catch (err) {
      setResult({ error: 'Network error — could not reach the server.' });
    } finally {
      setUploadingCsv(false);
    }
  };

  const isOutbound = kind === 'opposition_out';

  return (
    <div className="flex-1 min-h-0 overflow-y-auto px-7.5 pt-6 pb-15">
      <div className="mb-5">
        <h1 className="text-[19px] font-semibold m-0">Add to portfolio</h1>
      </div>

      <div className="max-w-[640px] flex items-center gap-1 bg-muted rounded-lg p-1 mb-5 w-fit">
        <button
          type="button"
          className={`px-3 py-1.5 rounded-md text-[13px] font-medium transition-colors ${
            !isOutbound ? 'bg-panel shadow-sm text-foreground' : 'text-muted-foreground hover:text-foreground'
          }`}
          onClick={() => selectKind('portfolio')}
        >
          My Applications
        </button>
        <button
          type="button"
          className={`px-3 py-1.5 rounded-md text-[13px] font-medium transition-colors ${
            isOutbound ? 'bg-panel shadow-sm text-foreground' : 'text-muted-foreground hover:text-foreground'
          }`}
          onClick={() => selectKind('opposition_out')}
        >
          Outbound Oppositions
        </button>
      </div>

      {result?.error && (
        <div className="max-w-[640px] rounded-lg px-4 py-3 text-[12.5px] mb-3 bg-status-bad-bg text-status-bad">{result.error}</div>
      )}
      {result && !result.error && (
        <div className="max-w-[640px] flex flex-col gap-2 mb-4">
          {result.added?.length > 0 && (
            <div className="rounded-lg px-4 py-3 text-[12.5px] bg-status-good-bg text-status-good">
              Added {result.added.length} {isOutbound ? 'opposition target' : 'trademark'}{result.added.length === 1 ? '' : 's'} — now queued for automatic fetch.
            </div>
          )}
          {result.skipped?.length > 0 && (
            <div className="rounded-lg px-4 py-3 text-[12.5px] bg-status-info-bg text-status-info">
              <div className="font-medium mb-1">
                {result.skipped.length} already tracked — not re-added:
              </div>
              <div className="flex flex-wrap gap-x-2 gap-y-1">
                {result.skipped.map((appNo: string) => (
                  <a
                    key={appNo}
                    href={isOutbound ? `/oppositions?tab=outbound&q=${appNo}` : `/trademarks?q=${appNo}`}
                    className="app-no-link"
                  >
                    {appNo}
                  </a>
                ))}
              </div>
            </div>
          )}
          {result.invalid?.length > 0 && (
            <div className="rounded-lg px-4 py-3 text-[12.5px] bg-status-warn-bg text-status-warn">
              {result.invalid.length} number{result.invalid.length === 1 ? '' : 's'} looked invalid and were skipped: {result.invalid.join(', ')}.
            </div>
          )}
        </div>
      )}

      <form onSubmit={submit}>
        <p className="text-[12.5px] text-muted-foreground mb-3 max-w-[560px] leading-relaxed">
          {isOutbound ? (
            <>Paste outbound opposition numbers your team is handling. Paste from Excel, CSV, comma-separated text, or one per line — we'll fetch status, class and owner details from the Registry automatically.</>
          ) : (
            <>Paste application numbers your team owns or manages. Paste from Excel, CSV, comma-separated text, or one per line — we'll fetch status, class and owner details from the Registry automatically.</>
          )}
        </p>

        <div className="mb-4 max-w-[640px]">
          <label className="block eyebrow text-muted-foreground! mb-1.5" htmlFor="app-no">
            {isOutbound ? 'Single opposition/application number' : 'Single application number'}
          </label>
          <input
            id="app-no"
            type="text"
            value={appNo}
            onChange={(e) => setAppNo(e.target.value)}
            placeholder="e.g. 1234567"
            inputMode="numeric"
            className="w-full border border-border-strong rounded-md px-2.5 py-2.5 font-mono text-[13px] text-foreground outline-none focus:border-primary"
          />
        </div>

        <div className="mb-4 max-w-[640px]">
          <label className="block eyebrow text-muted-foreground! mb-1.5" htmlFor="app-nos-bulk">Bulk add</label>
          <textarea
            id="app-nos-bulk"
            value={bulk}
            onChange={(e) => setBulk(e.target.value)}
            placeholder={'7936098\n7931056\n7918928'}
            className="w-full max-w-[640px] min-h-[200px] border border-border-strong rounded-lg p-3.5 font-mono text-[13px] leading-relaxed text-foreground resize-y outline-none focus:border-primary"
          />
        </div>

        <div className="max-w-[640px] mt-4 bg-status-good-bg text-status-good rounded-lg px-4 py-3 text-[12.5px] flex gap-2 items-start">
          <span>&#9432;</span>
          We'll pull current status tonight and check for updates every night after that — no need to add these again.
        </div>

        <div className="flex justify-end gap-2.5 max-w-[640px] mt-3.5">
          <button type="button" className="btn-ghost text-[13px] px-3 py-1.5 rounded-md" onClick={() => { setAppNo(''); setBulk(''); setResult(null); }}>Clear</button>
          <button type="submit" className="btn btn-accent" disabled={submitting}>
            {submitting ? 'Adding…' : isOutbound ? 'Add oppositions' : 'Add applications'}
          </button>
        </div>
      </form>

      <div className="max-w-[640px] mt-8 pt-6 border-t border-border">
        <h2 className="text-[15px] font-semibold mb-1.5">Large list? Upload a file instead</h2>
        <p className="text-[12.5px] text-muted-foreground mb-3 leading-relaxed">
          For big imports (hundreds or more) upload a CSV or Excel (.xlsx) file instead of pasting — these are
          added right away but fetched gradually in the background by the regular queue, instead of all at once,
          so it doesn't overload the registry lookup. Uses the {isOutbound ? 'Outbound Oppositions' : 'My Applications'} tab
          selected above.
        </p>
        <form onSubmit={submitCsv} className="flex items-center gap-2.5">
          <input
            ref={fileInputRef}
            type="file"
            accept=".csv,.txt,.xlsx,.xlsm"
            onChange={(e) => setCsvFile(e.target.files?.[0] ?? null)}
            className="flex-1 text-[12.5px] border border-border-strong rounded-md px-2.5 py-2 text-foreground outline-none focus:border-primary"
          />
          <button type="submit" className="btn btn-accent whitespace-nowrap" disabled={!csvFile || uploadingCsv}>
            {uploadingCsv ? 'Uploading…' : 'Upload File'}
          </button>
        </form>
      </div>
    </div>
  );
}
