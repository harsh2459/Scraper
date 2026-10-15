import { useEffect, useState } from 'react';
import { X, AlertTriangle } from 'lucide-react';
import Ring from './tm/Ring';

// In-app document/notice viewer — replaces the old <a target="_blank">
// pattern (open a raw file in a new browser tab) with a Gmail-style
// inline preview. Fetches the SAME authenticated backend route the old
// link pointed at (session cookies included automatically, same-origin —
// no new auth scheme), renders the response as a blob in an <iframe>.
// Works uniformly for both real PDFs and the HTML notice pages
// (reminders/correspondence) — browsers render both correctly inside an
// iframe pointed at a blob: URL, so no per-kind branching is needed here;
// only the caller's `url` prop differs.
export default function DocumentViewerModal({
  url,
  title,
  onClose,
}: {
  url: string;
  title: string;
  onClose: () => void;
}) {
  const [state, setState] = useState<'loading' | 'error' | 'ready'>('loading');
  const [blobUrl, setBlobUrl] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;

    setState('loading');
    fetch(url, { credentials: 'same-origin' })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.blob();
      })
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setBlobUrl(objectUrl);
        setState('ready');
      })
      .catch(() => {
        if (!cancelled) setState('error');
      });

    return () => {
      cancelled = true;
      // Revoke on unmount/url-change so repeatedly opening documents never
      // leaks blob URLs — each one holds its blob's memory alive until
      // revoked.
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [url]);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4 sm:p-8" onClick={onClose}>
      <div
        className="bg-panel rounded-lg shadow-xl w-full h-full max-w-[1100px] flex flex-col overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between gap-3 px-4 py-2.5 border-b border-border flex-shrink-0">
          <div className="text-[13.5px] font-medium truncate">{title}</div>
          <button type="button" className="text-muted-foreground hover:text-foreground flex-shrink-0" onClick={onClose}>
            <X size={18} />
          </button>
        </div>

        <div className="flex-1 min-h-0 relative bg-muted">
          {state === 'loading' && (
            <div className="absolute inset-0 flex items-center justify-center">
              <div className="loading"><Ring state="loading" size={28} /><span>Loading document…</span></div>
            </div>
          )}
          {state === 'error' && (
            <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-muted-foreground text-[13px]">
              <AlertTriangle size={22} />
              <span>Could not load this document.</span>
            </div>
          )}
          {state === 'ready' && blobUrl && (
            <iframe src={blobUrl} title={title} className="w-full h-full border-none" />
          )}
        </div>
      </div>
    </div>
  );
}
