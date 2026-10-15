import { useState, useEffect, useRef } from 'react';
import { X } from 'lucide-react';
import { API_BASE } from '../lib/api';

type TmOption = { id: number; app_no: string; word_mark?: string; label?: string };

export default function AddTaskModal({
  defaultDate,
  onClose,
  onCreated,
}: {
  defaultDate: string;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [title, setTitle] = useState('');
  const [dueDate, setDueDate] = useState(defaultDate);
  const [query, setQuery] = useState('');
  const [options, setOptions] = useState<TmOption[]>([]);
  const [selected, setSelected] = useState<TmOption | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const debounceRef = useRef<number | null>(null);

  useEffect(() => {
    if (!query.trim()) {
      setOptions([]);
      return;
    }
    if (debounceRef.current) window.clearTimeout(debounceRef.current);
    debounceRef.current = window.setTimeout(() => {
      fetch(`${API_BASE}/api/trademarks?q=${encodeURIComponent(query)}`)
        .then((res) => res.json())
        .then((json) => setOptions(json.trademarks || []))
        .catch(() => setOptions([]));
    }, 250);
    return () => { if (debounceRef.current) window.clearTimeout(debounceRef.current); };
  }, [query]);

  const canSubmit = title.trim().length > 0 && dueDate.length > 0 && !submitting;

  const submit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    setError('');
    try {
      const res = await fetch(`${API_BASE}/api/tasks`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: title.trim(),
          due_date: dueDate,
          my_trademark_id: selected?.id ?? null,
        }),
      });
      const json = await res.json();
      if (!res.ok) throw new Error(json.error || 'Could not create task.');
      onCreated();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not create task.');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4" onClick={onClose}>
      <div className="bg-panel rounded-lg shadow-xl w-full max-w-[440px] p-5" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-1">
          <div className="text-[15px] font-semibold">Add custom task</div>
          <button type="button" className="text-muted-foreground hover:text-foreground" onClick={onClose}><X size={16} /></button>
        </div>
        <p className="text-[12px] text-muted-foreground mb-4">Set a reminder for a trademark or opposition in your portfolio.</p>

        <label className="flex flex-col gap-1 mb-3">
          <span className="eyebrow text-muted-foreground!">Task title</span>
          <input
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="e.g. Call client"
            autoFocus
            className="border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary"
          />
        </label>

        <label className="flex flex-col gap-1 mb-3">
          <span className="eyebrow text-muted-foreground!">Due date</span>
          <input
            type="date"
            value={dueDate}
            onChange={(e) => setDueDate(e.target.value)}
            className="border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary"
          />
        </label>

        <div className="flex flex-col gap-1 mb-4 relative">
          <span className="eyebrow text-muted-foreground!">Trademark or opposition</span>
          <input
            type="text"
            value={selected ? `${selected.app_no} — ${selected.word_mark || selected.label || ''}` : query}
            onChange={(e) => { setSelected(null); setQuery(e.target.value); setPickerOpen(true); }}
            onFocus={() => setPickerOpen(true)}
            placeholder="Select trademark or opposition"
            className="border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary"
          />
          {pickerOpen && !selected && options.length > 0 && (
            <div className="absolute top-[calc(100%+2px)] left-0 right-0 z-10 bg-panel border border-border rounded-md shadow-lg max-h-[200px] overflow-y-auto">
              {options.map((opt) => (
                <button
                  key={opt.id}
                  type="button"
                  className="w-full text-left px-2.5 py-1.5 text-[12.5px] hover:bg-muted flex items-center gap-2"
                  onClick={() => { setSelected(opt); setPickerOpen(false); }}
                >
                  <span className="font-mono text-muted-foreground">{opt.app_no}</span>
                  <span className="truncate">{opt.word_mark || opt.label || '—'}</span>
                </button>
              ))}
            </div>
          )}
        </div>

        {error && <div className="text-[12.5px] text-status-bad mb-3">{error}</div>}

        <div className="flex justify-end gap-2">
          <button type="button" className="btn-ghost text-[13px] px-3 py-1.5 rounded-md" onClick={onClose}>Cancel</button>
          <button type="button" className="btn btn-primary disabled:opacity-50" disabled={!canSubmit} onClick={submit}>
            {submitting ? 'Adding…' : 'Add task'}
          </button>
        </div>
      </div>
    </div>
  );
}
