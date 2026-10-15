import { useEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown, Search } from 'lucide-react';

export interface SearchOption {
  value: string;
  label: string;
  /** Secondary text shown under the label and also searched (for example an email address). */
  hint?: string;
}

// A select you can type into. It filters as you type (name or hint), works with the arrow keys and
// Enter, and stays usable with hundreds of options, which a plain <select> is not.
export default function SearchSelect({ id, options, value, onChange, placeholder = 'Choose…', emptyText = 'No matches', disabled = false }: {
  id?: string;
  options: SearchOption[];
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  emptyText?: string;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const box = useRef<HTMLDivElement>(null);
  const selected = options.find((o) => o.value === value);

  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return options;
    return options.filter((o) => `${o.label} ${o.hint || ''}`.toLowerCase().includes(needle));
  }, [options, query]);

  useEffect(() => { setActive(0); }, [query, open]);
  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => { if (!box.current?.contains(event.target as Node)) { setOpen(false); setQuery(''); } };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, [open]);

  const choose = (option: SearchOption) => { onChange(option.value); setOpen(false); setQuery(''); };
  const onKey = (event: React.KeyboardEvent) => {
    if (event.key === 'ArrowDown') { event.preventDefault(); setOpen(true); setActive((i) => Math.min(shown.length - 1, i + 1)); }
    else if (event.key === 'ArrowUp') { event.preventDefault(); setActive((i) => Math.max(0, i - 1)); }
    else if (event.key === 'Enter' && open) { event.preventDefault(); if (shown[active]) choose(shown[active]); }
    else if (event.key === 'Escape') { setOpen(false); setQuery(''); }
  };

  return (
    <div ref={box} className="relative">
      <div className="relative">
        <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground pointer-events-none" />
        <input
          id={id}
          role="combobox"
          aria-expanded={open}
          aria-controls={id ? `${id}-list` : undefined}
          autoComplete="off"
          disabled={disabled}
          value={open ? query : selected ? selected.label : ''}
          placeholder={open && selected ? selected.label : placeholder}
          onFocus={() => setOpen(true)}
          onClick={() => setOpen(true)}
          onChange={(e) => { setQuery(e.target.value); setOpen(true); }}
          onKeyDown={onKey}
          className="w-full border border-border-strong rounded-md pl-8 pr-8 py-2 text-[13px] outline-none focus:border-primary bg-background"
        />
        <ChevronDown size={14} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-muted-foreground pointer-events-none" />
      </div>
      {open && (
        <ul id={id ? `${id}-list` : undefined} role="listbox" className="absolute z-30 mt-1 w-full max-h-64 overflow-y-auto bg-panel border border-border rounded-lg shadow-lg p-1">
          {shown.map((o, i) => (
            <li
              key={o.value}
              role="option"
              aria-selected={o.value === value}
              onMouseDown={(e) => { e.preventDefault(); choose(o); }}
              onMouseEnter={() => setActive(i)}
              className={`px-2.5 py-1.5 rounded-md cursor-pointer ${i === active ? 'bg-muted' : ''}`}
            >
              <div className={`text-[13px] truncate ${o.value === value ? 'font-medium' : ''}`}>{o.label}</div>
              {o.hint && <div className="text-[11.5px] text-muted-foreground truncate">{o.hint}</div>}
            </li>
          ))}
          {shown.length === 0 && <li className="px-2.5 py-2 text-[12.5px] text-muted-foreground">{emptyText}</li>}
        </ul>
      )}
    </div>
  );
}
