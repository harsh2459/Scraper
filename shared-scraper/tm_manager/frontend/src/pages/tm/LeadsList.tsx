import { useEffect, useState, useRef } from 'react';
import Ring from '../../components/tm/Ring';
import { useNavigate, Link } from 'react-router-dom';
import { Filter, Search, X, Pencil, Trash2 } from 'lucide-react';
import { tmApi } from '../../lib/tmApi';
import { kanbanStageClass, paymentStatusClass, formatDate, formatCurrency } from '../../lib/tmLead';
import Pagination from '../../components/Pagination';
import { useToast } from '../../components/tm/Toast';
import { useConfirm } from '../../components/tm/ConfirmDialog';

const ROWS_PER_PAGE = 50;

export default function LeadsList() {
  const navigate = useNavigate();
  const { showToast } = useToast();
  const confirmDialog = useConfirm();
  const [leads, setLeads] = useState<any[] | null>(null);
  const [q, setQ] = useState('');
  const [classFilter, setClassFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [paymentFilter, setPaymentFilter] = useState('');
  const [page, setPage] = useState(1);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const filtersRef = useRef<HTMLDivElement>(null);

  const load = () => {
    const params: Record<string, string> = {};
    if (q) params.q = q;
    if (classFilter) params.class = classFilter;
    if (statusFilter) params.status = statusFilter;
    if (paymentFilter) params.payment_status = paymentFilter;
    tmApi.listLeads(params).then((json) => setLeads(json.leads)).catch((err) => console.error(err));
  };

  useEffect(load, [q, classFilter, statusFilter, paymentFilter]);

  const deleteLead = async (lead: any) => {
    const ok = await confirmDialog({
      title: `Delete lead "${lead.name}"?`,
      message: 'This cannot be undone.',
      confirmLabel: 'Delete',
      danger: true,
    });
    if (!ok) return;
    setDeletingId(lead.id);
    try {
      await tmApi.deleteLead(lead.id);
      load();
      showToast('success', `Deleted lead "${lead.name}".`);
    } catch (err: any) {
      showToast('error', err.message || 'Failed to delete lead');
    } finally {
      setDeletingId(null);
    }
  };

  useEffect(() => {
    function onClickOutside(e: MouseEvent) {
      if (filtersRef.current && !filtersRef.current.contains(e.target as Node)) setFiltersOpen(false);
    }
    if (filtersOpen) document.addEventListener('mousedown', onClickOutside);
    return () => document.removeEventListener('mousedown', onClickOutside);
  }, [filtersOpen]);

  const activeFilterCount = [classFilter, statusFilter, paymentFilter].filter(Boolean).length;
  const clearFilters = () => { setClassFilter(''); setStatusFilter(''); setPaymentFilter(''); setPage(1); };

  if (!leads) return <div className="loading"><Ring state="loading" size={28} /><span>Loading leads…</span></div>;

  const totalPages = Math.max(1, Math.ceil(leads.length / ROWS_PER_PAGE));
  const currentPage = Math.min(page, totalPages);
  const pageRows = leads.slice((currentPage - 1) * ROWS_PER_PAGE, currentPage * ROWS_PER_PAGE);
  const availableClasses = Array.from(new Set(leads.map((l) => l.class).filter(Boolean))).sort();
  const availableStatuses = Array.from(new Set(leads.map((l) => l.status).filter(Boolean))).sort();

  return (
    <div className="flex h-full w-full overflow-hidden relative">
      <div className="flex-1 min-w-0 flex flex-col overflow-hidden min-h-0">
        <div className="flex items-center justify-between gap-3 px-5 py-3 border-b border-border bg-panel flex-wrap">
          <div className="flex items-center gap-2.5 flex-1 min-w-[320px]">
            <div className="font-display font-semibold text-[15px] whitespace-nowrap">Leads</div>
            <div className="flex items-center border border-border-strong rounded-md overflow-hidden flex-1 max-w-[420px]">
              <span className="flex items-center px-2.5 text-muted-foreground"><Search size={13} /></span>
              <input
                type="text"
                value={q}
                onChange={(e) => { setQ(e.target.value); setPage(1); }}
                placeholder="Search name, mobile, trademark, ARN…"
                className="border-none flex-1 text-[13px] py-1.5 pr-2.5 outline-none"
              />
            </div>
          </div>
          <div className="flex items-center gap-2">
            <div className="relative" ref={filtersRef}>
              <button type="button" className={`btn ${activeFilterCount ? 'border-primary text-primary' : ''}`} onClick={() => setFiltersOpen((v) => !v)}>
                <Filter size={12} /> Filters{activeFilterCount ? ` (${activeFilterCount})` : ''}
              </button>
              {filtersOpen && (
                <div className="absolute right-0 top-[calc(100%+6px)] z-20 w-[280px] bg-panel border border-border rounded-lg shadow-lg p-4 flex flex-col gap-3.5">
                  <div className="flex items-center justify-between">
                    <span className="text-[13px] font-semibold">Filters</span>
                    {activeFilterCount > 0 && (
                      <button type="button" className="text-[12px] text-muted-foreground hover:text-primary flex items-center gap-1" onClick={clearFilters}>
                        <X size={12} /> Clear all
                      </button>
                    )}
                  </div>
                  <label className="flex flex-col gap-1.5">
                    <span className="eyebrow text-muted-foreground!">Class</span>
                    <select value={classFilter} onChange={(e) => { setClassFilter(e.target.value); setPage(1); }} className="border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary">
                      <option value="">All classes</option>
                      {availableClasses.map((c) => <option key={c} value={c}>Class {c}</option>)}
                    </select>
                  </label>
                  <label className="flex flex-col gap-1.5">
                    <span className="eyebrow text-muted-foreground!">Status</span>
                    <select value={statusFilter} onChange={(e) => { setStatusFilter(e.target.value); setPage(1); }} className="border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary">
                      <option value="">All statuses</option>
                      {availableStatuses.map((s) => <option key={s} value={s}>{s}</option>)}
                    </select>
                  </label>
                  <label className="flex flex-col gap-1.5">
                    <span className="eyebrow text-muted-foreground!">Payment</span>
                    <select value={paymentFilter} onChange={(e) => { setPaymentFilter(e.target.value); setPage(1); }} className="border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary">
                      <option value="">All</option>
                      <option value="Payment Pending">Payment Pending</option>
                      <option value="Payment Done">Payment Done</option>
                    </select>
                  </label>
                </div>
              )}
            </div>
            <Link className="btn" to="/tm/leads/kanban">Kanban</Link>
            <Link className="btn btn-accent" to="/tm/leads/create">+ New Lead</Link>
          </div>
        </div>

        <div className="flex-1 overflow-auto bg-panel">
          <table className="w-full min-w-[900px] border-collapse">
            <thead>
              <tr>
                {['Name', 'Mobile', 'Trademark', 'Class', 'Stage', 'Payment', 'Pricing', 'Lead Date', ''].map((h) => (
                  <th key={h} className="text-left text-[12.5px] font-medium px-2.5 py-2 border-b border-border sticky top-0 bg-panel z-10 whitespace-nowrap">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {pageRows.map((lead) => (
                <tr key={lead.id} onClick={() => navigate(`/tm/leads/${lead.id}`)} className="cursor-pointer hover:bg-muted">
                  <td className="px-2.5 py-1.5 border-b border-border align-middle text-[13px] font-medium">{lead.name}</td>
                  <td className="px-2.5 py-1.5 border-b border-border align-middle text-[13px] font-mono text-muted-foreground">{lead.mobile || '—'}</td>
                  <td className="px-2.5 py-1.5 border-b border-border align-middle text-[13px]">{lead.proposed_trademark || '—'}</td>
                  <td className="px-2.5 py-1.5 border-b border-border align-middle text-[13px] text-muted-foreground">{lead.class || '—'}</td>
                  <td className="px-2.5 py-1.5 border-b border-border align-middle text-[13px]">
                    <span className={`pill ${kanbanStageClass(lead.kanban_stage)}`}><span className="dot" />{lead.kanban_stage}</span>
                  </td>
                  <td className="px-2.5 py-1.5 border-b border-border align-middle text-[13px]">
                    <span className={`pill ${paymentStatusClass(lead.payment_status)}`}><span className="dot" />{lead.payment_status}</span>
                  </td>
                  <td className="px-2.5 py-1.5 border-b border-border align-middle text-[13px] font-mono">{formatCurrency(lead.pricing)}</td>
                  <td className="px-2.5 py-1.5 border-b border-border align-middle text-[13px] font-mono text-muted-foreground">{formatDate(lead.lead_date)}</td>
                  <td className="px-2.5 py-1.5 border-b border-border align-middle text-[13px]" onClick={(e) => e.stopPropagation()}>
                    <div className="flex items-center gap-1.5">
                      <button
                        type="button"
                        className="w-7 h-7 rounded-md border border-border-strong flex items-center justify-center hover:bg-muted"
                        title="Edit lead"
                        onClick={() => navigate(`/tm/leads/${lead.id}`)}
                      >
                        <Pencil size={13} />
                      </button>
                      <button
                        type="button"
                        className="w-7 h-7 rounded-md border border-border-strong flex items-center justify-center hover:bg-status-bad-bg hover:border-status-bad hover:text-status-bad disabled:opacity-50"
                        title="Delete lead"
                        disabled={deletingId === lead.id}
                        onClick={() => deleteLead(lead)}
                      >
                        <Trash2 size={13} />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
              {pageRows.length === 0 && (
                <tr><td colSpan={9} className="text-center py-10 text-muted-foreground">No leads found.</td></tr>
              )}
            </tbody>
          </table>
        </div>

        <Pagination
          page={currentPage}
          totalPages={totalPages}
          totalRows={leads.length}
          pageRows={ROWS_PER_PAGE}
          rowLabel="lead(s)"
          onPrev={() => setPage((p) => Math.max(1, p - 1))}
          onNext={() => setPage((p) => Math.min(totalPages, p + 1))}
        />
      </div>
    </div>
  );
}
