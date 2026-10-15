import { useEffect, useState } from 'react';
import { X } from 'lucide-react';

// Small building blocks shared by the administration pages. They exist so every page explains
// itself the same way: plain words, a one-line intro, and technical detail tucked away.

export function timeAgo(value: string | null | undefined): string {
  if (!value) return 'never';
  const seconds = Math.round((Date.now() - Date.parse(value)) / 1000);
  if (Number.isNaN(seconds)) return '—';
  if (seconds < 45) return 'just now';
  if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  if (seconds < 86400 * 14) return `${Math.round(seconds / 86400)} d ago`;
  return new Date(value).toLocaleDateString();
}

export function clock(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '—';
}

export function bytes(value: number | null | undefined): string {
  if (typeof value !== 'number') return '—';
  return value >= 1e9 ? `${(value / 1e9).toFixed(1)} GB` : `${Math.round(value / 1e6)} MB`;
}

export function PageIntro({ children, action }: { children: React.ReactNode; action?: React.ReactNode }) {
  return <div className="ops-intro"><p>{children}</p>{action}</div>;
}

export function Tone({ tone, children }: { tone?: 'good' | 'bad' | 'warn' | ''; children: React.ReactNode }) {
  return <span className={`ops-badge ${tone || ''}`}><i />{children}</span>;
}

export function Drawer({ title, subtitle, onClose, children }: { title: string; subtitle?: React.ReactNode; onClose: () => void; children: React.ReactNode }) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  return <div className="ops-drawer-backdrop" onClick={onClose}>
    <aside className="ops-drawer" role="dialog" aria-modal="true" aria-label={title} onClick={e => e.stopPropagation()}>
      <header><div><h2>{title}</h2>{subtitle && <div className="ops-muted">{subtitle}</div>}</div><button className="ops-icon-button" onClick={onClose} aria-label="Close"><X size={16} /></button></header>
      <div className="ops-drawer-body">{children}</div>
    </aside>
  </div>;
}

export function Section({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return <section className="ops-section"><h3>{title}</h3>{hint && <p className="ops-muted">{hint}</p>}{children}</section>;
}

export function Facts({ items }: { items: [string, React.ReactNode][] }) {
  return <dl className="ops-facts">{items.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>;
}

export function CopyBox({ text, label = 'Copy' }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return <div className="ops-codebox">
    <pre>{text}</pre>
    <button className="ops-button secondary" onClick={async () => { try { await navigator.clipboard.writeText(text); setDone(true); setTimeout(() => setDone(false), 2000); } catch { /* clipboard blocked */ } }}>{done ? 'Copied' : label}</button>
  </div>;
}

export function ActionRow({ title, text, children }: { title: string; text: string; children: React.ReactNode }) {
  return <div className="ops-action-row"><div><strong>{title}</strong><p className="ops-muted">{text}</p></div><div>{children}</div></div>;
}
