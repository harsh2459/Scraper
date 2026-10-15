import { createContext, useCallback, useContext, useRef, useState } from 'react';
import { AlertTriangle } from 'lucide-react';

interface ConfirmOptions {
  title: string;
  message?: string;
  confirmLabel?: string;
  danger?: boolean;
}

const ConfirmContext = createContext<((opts: ConfirmOptions) => Promise<boolean>) | null>(null);

// Replaces bare `confirm()` calls with an actual modal — the returned
// promise resolves true/false the same way `confirm()` did, so call sites
// just add `await` instead of restructuring around a callback.
export function ConfirmProvider({ children }: { children: React.ReactNode }) {
  const [opts, setOpts] = useState<ConfirmOptions | null>(null);
  const resolver = useRef<(value: boolean) => void>(() => {});

  const confirmDialog = useCallback((options: ConfirmOptions) => {
    setOpts(options);
    return new Promise<boolean>((resolve) => { resolver.current = resolve; });
  }, []);

  const close = (result: boolean) => {
    resolver.current(result);
    setOpts(null);
  };

  return (
    <ConfirmContext.Provider value={confirmDialog}>
      {children}
      {opts && (
        <div className="fixed inset-0 z-[110] flex items-center justify-center bg-black/40 print:hidden" onClick={() => close(false)}>
          <div className="bg-panel border border-border rounded-lg shadow-xl w-[380px] p-5" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-start gap-3 mb-4">
              <div className={`w-9 h-9 rounded-full flex items-center justify-center flex-shrink-0 ${opts.danger ? 'bg-status-bad-bg text-status-bad' : 'bg-status-info-bg text-status-info'}`}>
                <AlertTriangle size={17} />
              </div>
              <div>
                <div className="text-[14.5px] font-semibold">{opts.title}</div>
                {opts.message && <div className="text-[12.5px] text-muted-foreground mt-1">{opts.message}</div>}
              </div>
            </div>
            <div className="flex justify-end gap-2">
              <button type="button" className="btn" onClick={() => close(false)}>Cancel</button>
              <button
                type="button"
                className={`btn ${opts.danger ? 'bg-status-bad text-white border-status-bad hover:opacity-90' : 'btn-primary'}`}
                onClick={() => close(true)}
              >
                {opts.confirmLabel || 'Confirm'}
              </button>
            </div>
          </div>
        </div>
      )}
    </ConfirmContext.Provider>
  );
}

export function useConfirm() {
  const ctx = useContext(ConfirmContext);
  if (!ctx) throw new Error('useConfirm must be used within a ConfirmProvider');
  return ctx;
}
