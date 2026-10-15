import { useState, useEffect, useMemo } from 'react';
import Ring from '../components/tm/Ring';
import MarkThumb from '../components/MarkThumb';
import { Search } from 'lucide-react';
import { API_BASE } from '../lib/api';
import Pagination from '../components/Pagination';
import TrademarkOverlay from '../components/TrademarkOverlay';
import DocumentViewerModal from '../components/DocumentViewerModal';

const PAGE_SIZE = 100;

export default function NewDocuments() {
  const [data, setData] = useState<any>(null);
  const [q, setQ] = useState('');
  const [page, setPage] = useState(1);
  const [openTmId, setOpenTmId] = useState<number | null>(null);
  const [viewerDoc, setViewerDoc] = useState<{ url: string; title: string } | null>(null);

  useEffect(() => {
    fetch(`${API_BASE}/api/new-documents`)
      .then((res) => res.json())
      .then((json) => setData(json))
      .catch((err) => console.error(err));
  }, []);

  const allDocs = useMemo(() => data?.documents ?? [], [data]);
  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return allDocs;
    return allDocs.filter((d: any) =>
      (d.word_mark || d.label || '').toLowerCase().includes(needle) ||
      (d.app_no || '').toLowerCase().includes(needle) ||
      (d.subject || '').toLowerCase().includes(needle)
    );
  }, [allDocs, q]);
  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const pageDocs = useMemo(
    () => filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE),
    [filtered, page],
  );

  if (!data) return <div className="loading"><Ring state="loading" size={28} /><span>Loading documents…</span></div>;

  return (
    <div className="flex-1 min-h-0 flex flex-col overflow-hidden">
      <div className="flex items-center justify-between gap-3 px-5 py-3 border-b border-border bg-panel flex-wrap flex-shrink-0">
        <div className="font-display font-semibold text-[15px]">Documents Updates</div>
        <div className="flex items-center border border-border-strong rounded-md overflow-hidden max-w-[360px] flex-1 min-w-[220px]">
          <span className="flex items-center px-2.5 text-muted-foreground"><Search size={13} /></span>
          <input
            type="text"
            value={q}
            onChange={(e) => { setQ(e.target.value); setPage(1); }}
            placeholder="Search mark, app number or document…"
            className="border-none flex-1 text-[13px] py-1.5 pr-2.5 outline-none"
          />
        </div>
      </div>

      {filtered.length ? (
        <>
          <div className="flex-1 min-h-0 overflow-y-auto px-7.5 pt-4">
            <div className="table-panel">
              <table className="w-full border-separate border-spacing-0">
                <thead>
                  <tr>
                    <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Trademark</th>
                    <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Document</th>
                    <th style={{ borderBottom: '1px dashed var(--color-border)' }} className="text-left text-[12.5px] font-medium px-2.5 py-2 sticky top-0 bg-panel">Despatch Date</th>
                  </tr>
                </thead>
                <tbody>
                  {pageDocs.map((d: any, idx: number) => (
                    <tr key={idx} className="hover:bg-muted">
                      <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2 text-[13px]">
                        <div className="flex items-center gap-2.5">
                          <MarkThumb id={d.tm_id} hasLogo={d.has_logo} name={d.word_mark || d.label || d.app_no} />
                          <div>
                            <button type="button" onClick={() => setOpenTmId(d.tm_id)} className="app-no-link">{d.app_no}</button>
                            <div className="font-medium">{d.word_mark || d.label || '—'}</div>
                          </div>
                        </div>
                      </td>
                      <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2 text-[13px] font-medium">
                        {d.document_path ? (
                          <button
                            type="button"
                            onClick={() => setViewerDoc({
                              url: `${API_BASE}/trademark/${d.tm_id}/notice/correspondence/${encodeURIComponent(d.document_path)}`,
                              title: d.subject || 'Correspondence',
                            })}
                            className="text-primary hover:underline text-left"
                          >
                            {d.subject}
                          </button>
                        ) : (
                          d.subject
                        )}
                      </td>
                      <td style={{ borderBottom: '1px dashed var(--color-border)' }} className="px-2.5 py-2 text-[12.5px] text-muted-foreground font-mono">{d.despatch_date || d.corres_date || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
          <Pagination
            page={page}
            totalPages={totalPages}
            totalRows={filtered.length}
            pageRows={PAGE_SIZE}
            rowLabel="documents"
            onPrev={() => setPage((p) => Math.max(1, p - 1))}
            onNext={() => setPage((p) => Math.min(totalPages, p + 1))}
          />
        </>
      ) : (
        <div className="py-10 text-center text-muted-foreground text-[13.5px]">
          {q ? 'No documents match your search.' : 'No documents on file yet.'}
        </div>
      )}
      {openTmId && <TrademarkOverlay tmId={openTmId} onClose={() => setOpenTmId(null)} />}
      {viewerDoc && (
        <DocumentViewerModal url={viewerDoc.url} title={viewerDoc.title} onClose={() => setViewerDoc(null)} />
      )}
    </div>
  );
}
