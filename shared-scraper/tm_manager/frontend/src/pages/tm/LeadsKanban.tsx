import { useEffect, useState } from 'react';
import Ring from '../../components/tm/Ring';
import { useNavigate } from 'react-router-dom';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { tmApi } from '../../lib/tmApi';
import { KANBAN_STAGES, formatCurrency } from '../../lib/tmLead';

const OVERRIDE_KEY = 'tm_kanban_collapsed_overrides';

// Empty stages start collapsed and non-empty ones start expanded — but once
// a user explicitly toggles a column, that choice ("expanded" or
// "collapsed") sticks regardless of how many leads land in it later, so an
// override map (not a plain collapsed-set) is what persists.
function loadOverrides(): Record<string, boolean> {
  try {
    const raw = localStorage.getItem(OVERRIDE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}
function saveOverrides(map: Record<string, boolean>) {
  try { localStorage.setItem(OVERRIDE_KEY, JSON.stringify(map)); } catch { /* ignore */ }
}

export default function LeadsKanban() {
  const navigate = useNavigate();
  const [grouped, setGrouped] = useState<Record<string, any[]> | null>(null);
  const [dragLeadId, setDragLeadId] = useState<number | null>(null);
  const [dragOverStage, setDragOverStage] = useState<string | null>(null);
  const [overrides, setOverrides] = useState<Record<string, boolean>>(() => loadOverrides());

  const load = () => tmApi.kanban().then((json) => setGrouped(json.grouped)).catch((err) => console.error(err));

  useEffect(() => { load(); }, []);

  const isCollapsedFor = (stage: string, rowCount: number) =>
    stage in overrides ? overrides[stage] : rowCount === 0;

  const toggleCollapsed = (stage: string, rowCount: number) => {
    setOverrides((prev) => {
      const currentlyCollapsed = isCollapsedFor(stage, rowCount);
      const next = { ...prev, [stage]: !currentlyCollapsed };
      saveOverrides(next);
      return next;
    });
  };

  const onDrop = async (stage: string) => {
    setDragOverStage(null);
    if (dragLeadId == null || !grouped) return;
    const currentStage = Object.keys(grouped).find((s) => grouped[s].some((l) => l.id === dragLeadId));
    if (currentStage === stage) { setDragLeadId(null); return; }

    // Optimistic move so the card doesn't snap back while the request is
    // in flight — reconciled with the server's real grouping right after.
    setGrouped((prev) => {
      if (!prev) return prev;
      const next: Record<string, any[]> = {};
      let moved: any = null;
      for (const s of Object.keys(prev)) {
        next[s] = prev[s].filter((l) => {
          if (l.id === dragLeadId) { moved = l; return false; }
          return true;
        });
      }
      if (moved) next[stage] = [...(next[stage] || []), { ...moved, kanban_stage: stage }];
      return next;
    });

    try {
      await tmApi.kanbanUpdate(dragLeadId, stage);
    } finally {
      load();
      setDragLeadId(null);
    }
  };

  if (!grouped) return <div className="loading"><Ring state="loading" size={28} /><span>Loading kanban…</span></div>;

  return (
    <div className="flex-1 min-h-0 overflow-x-auto overflow-y-hidden px-5 py-4 bg-background">
      <div className="flex gap-3 h-full min-w-max">
        {KANBAN_STAGES.map((stage) => {
          const rows = grouped[stage] || [];
          const isCollapsed = isCollapsedFor(stage, rows.length);
          return (
            <div
              key={stage}
              className={`flex-shrink-0 flex flex-col bg-muted/40 rounded-lg border ${dragOverStage === stage ? 'border-primary' : 'border-border'} ${isCollapsed ? 'w-[44px]' : 'w-[260px]'}`}
              onDragOver={(e) => { e.preventDefault(); setDragOverStage(stage); }}
              onDragLeave={() => setDragOverStage((s) => (s === stage ? null : s))}
              onDrop={() => onDrop(stage)}
            >
              {isCollapsed ? (
                <button
                  type="button"
                  onClick={() => toggleCollapsed(stage, rows.length)}
                  className="flex flex-col items-center gap-2 py-3 px-1 h-full hover:bg-muted rounded-lg"
                  title={`Expand ${stage}`}
                >
                  <ChevronRight size={14} className="text-muted-foreground flex-shrink-0" />
                  <span className="text-[11px] font-mono text-muted-foreground bg-panel px-1 py-0.5 rounded flex-shrink-0">{rows.length}</span>
                  <span className="text-[11.5px] font-semibold [writing-mode:vertical-rl] rotate-180 truncate">{stage}</span>
                </button>
              ) : (
                <>
                  <div className="px-3 py-2.5 border-b border-border flex items-center gap-1.5 flex-shrink-0">
                    <button type="button" onClick={() => toggleCollapsed(stage, rows.length)} className="text-muted-foreground hover:text-foreground flex-shrink-0" title="Collapse">
                      <ChevronLeft size={14} />
                    </button>
                    <span className="text-[12.5px] font-semibold truncate flex-1">{stage}</span>
                    <span className="text-[11px] font-mono text-muted-foreground bg-panel px-1.5 py-0.5 rounded flex-shrink-0">{rows.length}</span>
                  </div>
                  <div className="flex-1 overflow-y-auto p-2 flex flex-col gap-2 min-h-[80px]">
                    {rows.map((lead) => (
                      <div
                        key={lead.id}
                        draggable
                        onDragStart={() => setDragLeadId(lead.id)}
                        onClick={() => navigate(`/tm/leads/${lead.id}`)}
                        className="bg-panel border border-border rounded-md p-2.5 cursor-pointer hover:border-border-strong shadow-sm"
                      >
                        <div className="text-[12.5px] font-medium truncate">{lead.name}</div>
                        <div className="text-[11.5px] text-muted-foreground truncate mt-0.5">{lead.proposed_trademark || '—'}</div>
                        <div className="flex items-center justify-between mt-1.5">
                          <span className="text-[10.5px] font-mono text-muted-foreground">{lead.class ? `Class ${lead.class}` : ''}</span>
                          <span className="text-[10.5px] font-mono font-medium">{formatCurrency(lead.pricing)}</span>
                        </div>
                      </div>
                    ))}
                    {rows.length === 0 && <div className="text-[11px] text-muted-foreground text-center py-4">No leads</div>}
                  </div>
                </>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
