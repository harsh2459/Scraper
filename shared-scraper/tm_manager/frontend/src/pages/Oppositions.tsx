import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import Trademarks from './Trademarks';

// Inbound: every portfolio trademark whose real IP India portal status
// contains "Opposed" — someone else opposed a mark you own. There is no
// separate inbound opposition record anywhere in the schema, this is just
// the existing portfolio list filtered to that one status.
//
// Outbound: applications your team is opposing that you don't own — tracked
// as their own my_trademarks rows (kind='opposition_out', see
// MY_TRADEMARKS_SCHEMA), added from Add to portfolio's "Outbound
// Oppositions" tab and auto-fetched/refreshed by the exact same pipeline as
// portfolio marks, so this list behaves identically to Inbound (daily
// refresh, live status, same table/columns) — just a different bucket, not
// a different feature.
export default function Oppositions() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [tab, setTab] = useState<'inbound' | 'outbound'>(
    searchParams.get('tab') === 'outbound' ? 'outbound' : 'inbound'
  );

  const selectTab = (next: 'inbound' | 'outbound') => {
    setTab(next);
    setSearchParams(next === 'outbound' ? { tab: 'outbound' } : {});
  };

  return (
    <div className="flex h-full w-full overflow-hidden flex-col">
      <div className="flex items-center gap-1 px-5 pt-3 border-b border-border bg-panel">
        <button
          type="button"
          className={`px-3 py-1.5 rounded-t-md text-[13px] font-medium border border-b-0 ${
            tab === 'inbound'
              ? 'bg-background border-border text-foreground'
              : 'border-transparent text-muted-foreground hover:text-foreground'
          }`}
          onClick={() => selectTab('inbound')}
        >
          Inbound
        </button>
        <button
          type="button"
          className={`px-3 py-1.5 rounded-t-md text-[13px] font-medium border border-b-0 ${
            tab === 'outbound'
              ? 'bg-background border-border text-foreground'
              : 'border-transparent text-muted-foreground hover:text-foreground'
          }`}
          onClick={() => selectTab('outbound')}
        >
          Outbound
        </button>
      </div>
      <div className="flex-1 min-h-0">
        {tab === 'inbound' ? (
          // key forces a full remount on tab switch — without it, React
          // keeps the same Trademarks instance across both branches (same
          // component type, same position in the tree) and its `data`
          // state doesn't reset just because the kind/lockedStatus props
          // changed. That left the PREVIOUS tab's rows (or its "No
          // trademarks found" empty state) on screen until the new fetch
          // resolved, instead of showing the loading spinner Trademarks
          // already renders correctly whenever data is null.
          <Trademarks key="inbound" title="Oppositions — Inbound" lockedStatus="Opposed" />
        ) : (
          <Trademarks key="outbound" title="Oppositions — Outbound" kind="opposition_out" />
        )}
      </div>
    </div>
  );
}
