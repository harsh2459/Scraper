import { useEffect, useState, useMemo } from 'react';
import Ring from '../../components/tm/Ring';
import { useNavigate, Link } from 'react-router-dom';
import { Search, Printer, Download, Plus, X } from 'lucide-react';
import { tmApi } from '../../lib/tmApi';
import { invoiceNumber, invoiceTotal, paymentStatusClass, formatDate } from '../../lib/tmLead';
import { downloadInvoicePdf } from '../../lib/invoicePdf';
import { useToast } from '../../components/tm/Toast';

export default function InvoicesList() {
  const navigate = useNavigate();
  const { showToast } = useToast();
  const [leads, setLeads] = useState<any[] | null>(null);
  const [q, setQ] = useState('');
  const [paymentFilter, setPaymentFilter] = useState('');
  const [settings, setSettings] = useState<any>(null);
  const [downloadingId, setDownloadingId] = useState<number | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [pickerQ, setPickerQ] = useState('');
  const [allLeads, setAllLeads] = useState<any[]>([]);

  useEffect(() => {
    const params: Record<string, string> = {};
    if (q) params.q = q;
    if (paymentFilter) params.payment_status = paymentFilter;
    tmApi.invoicesList(params).then((json) => setLeads(json.leads)).catch((err) => console.error(err));
  }, [q, paymentFilter]);

  // For the "New Invoice" lead picker — load the full lead list once when opened.
  useEffect(() => {
    if (pickerOpen && allLeads.length === 0) {
      tmApi.listLeads({}).then((json: any) => setAllLeads(json.leads || [])).catch(() => {});
    }
  }, [pickerOpen, allLeads.length]);

  const pickerResults = useMemo(() => {
    const n = pickerQ.trim().toLowerCase();
    const base = n
      ? allLeads.filter((l) =>
          [l.name, l.business_name, l.proposed_trademark, l.mobile]
            .filter(Boolean)
            .some((v: string) => v.toLowerCase().includes(n)))
      : allLeads;
    return base.slice(0, 30);
  }, [pickerQ, allLeads]);

  useEffect(() => { tmApi.getSettings().then(setSettings).catch(() => {}); }, []);

  if (!leads) return <div className="loading"><Ring state="loading" size={28} /><span>Loading invoices…</span></div>;

  // Print still needs the invoice's own page (company branding, editable
  // line items) — jumping there with ?print=1 lets InvoiceView auto-open
  // the print dialog once it has actually rendered.
  const openForPrint = (leadId: number) => navigate(`/tm/leads/${leadId}/invoice?print=1`);

  // Download, unlike print, doesn't need to leave the list — it renders a
  // static copy of the invoice off-screen and saves a .pdf file directly.
  const downloadPdf = async (lead: any) => {
    if (!settings) return;
    setDownloadingId(lead.id);
    try {
      await downloadInvoicePdf(lead, settings);
    } catch (err: any) {
      showToast('error', err.message || 'Failed to generate PDF');
    } finally {
      setDownloadingId(null);
    }
  };

  return (
    <div className="flex h-full w-full overflow-hidden">
      <div className="flex-1 min-w-0 flex flex-col overflow-hidden min-h-0">
        <div className="flex items-center justify-between gap-3 px-5 py-3 border-b border-border bg-panel flex-wrap">
          <div className="flex items-center gap-2.5 flex-1 min-w-[320px]">
            <div className="font-display font-semibold text-[15px] whitespace-nowrap">Invoices</div>
            <div className="flex items-center border border-border-strong rounded-md overflow-hidden flex-1 max-w-[420px]">
              <span className="flex items-center px-2.5 text-muted-foreground"><Search size={13} /></span>
              <input
                type="text"
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Search name, business, trademark…"
                className="border-none flex-1 text-[13px] py-1.5 pr-2.5 outline-none"
              />
            </div>
          </div>
          <div className="flex items-center gap-2">
            <select
              value={paymentFilter}
              onChange={(e) => setPaymentFilter(e.target.value)}
              className="border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary"
            >
              <option value="">All payments</option>
              <option value="Payment Pending">Payment Pending</option>
              <option value="Payment Done">Payment Done</option>
            </select>
            <button type="button" className="btn btn-accent whitespace-nowrap" onClick={() => setPickerOpen(true)}>
              <Plus size={13} /> New Invoice
            </button>
          </div>
        </div>

        <div className="flex-1 overflow-auto bg-panel">
          <table className="w-full min-w-[820px] border-collapse">
            <thead>
              <tr>
                {['Invoice #', 'Client', 'Trademark', 'Amount', 'Payment', 'Date', ''].map((h) => (
                  <th key={h} className="text-left text-[12.5px] font-medium px-2.5 py-2 border-b border-border sticky top-0 bg-panel z-10 whitespace-nowrap">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {leads.map((lead) => (
                <tr key={lead.id} onClick={() => navigate(`/tm/leads/${lead.id}/invoice`)} className="cursor-pointer hover:bg-muted">
                  <td className="px-2.5 py-1.5 border-b border-border align-middle text-[13px] font-mono">{invoiceNumber(lead)}</td>
                  <td className="px-2.5 py-1.5 border-b border-border align-middle text-[13px] font-medium">{lead.business_name || lead.name}</td>
                  <td className="px-2.5 py-1.5 border-b border-border align-middle text-[13px]">{lead.proposed_trademark || '—'}</td>
                  <td className="px-2.5 py-1.5 border-b border-border align-middle text-[13px] font-mono">{invoiceTotal(lead).toLocaleString('en-IN', { style: 'currency', currency: 'INR', minimumFractionDigits: 2 })}</td>
                  <td className="px-2.5 py-1.5 border-b border-border align-middle text-[13px]">
                    <span className={`pill ${paymentStatusClass(lead.payment_status)}`}><span className="dot" />{lead.payment_status}</span>
                  </td>
                  <td className="px-2.5 py-1.5 border-b border-border align-middle text-[13px] font-mono text-muted-foreground">{formatDate(lead.created_at)}</td>
                  <td className="px-2.5 py-1.5 border-b border-border align-middle text-[13px]" onClick={(e) => e.stopPropagation()}>
                    <div className="flex items-center gap-1.5">
                      <button
                        type="button"
                        className="w-7 h-7 rounded-md border border-border-strong flex items-center justify-center hover:bg-muted"
                        title="Print invoice"
                        onClick={() => openForPrint(lead.id)}
                      >
                        <Printer size={13} />
                      </button>
                      <button
                        type="button"
                        className="w-7 h-7 rounded-md border border-border-strong flex items-center justify-center hover:bg-muted disabled:opacity-50"
                        title="Download invoice"
                        disabled={downloadingId === lead.id}
                        onClick={() => downloadPdf(lead)}
                      >
                        <Download size={13} />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
              {leads.length === 0 && (
                <tr><td colSpan={7} className="text-center py-10 text-muted-foreground">No invoices found.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {pickerOpen && (
        <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 p-4 pt-[10vh]" onClick={() => setPickerOpen(false)}>
          <div className="bg-panel border border-border rounded-lg shadow-xl w-full max-w-[520px] flex flex-col max-h-[70vh]" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between px-4 py-3 border-b border-border">
              <div className="font-display font-semibold text-[15px]">New Invoice — pick a lead</div>
              <button type="button" onClick={() => setPickerOpen(false)} className="text-muted-foreground hover:text-foreground"><X size={16} /></button>
            </div>
            <div className="px-4 py-3 border-b border-border">
              <div className="flex items-center border border-border-strong rounded-md overflow-hidden">
                <span className="flex items-center px-2.5 text-muted-foreground"><Search size={13} /></span>
                <input
                  autoFocus
                  type="text"
                  value={pickerQ}
                  onChange={(e) => setPickerQ(e.target.value)}
                  placeholder="Search lead by name, business, trademark, mobile…"
                  className="border-none flex-1 text-[13px] py-1.5 pr-2.5 outline-none"
                />
              </div>
              <div className="mt-2 text-[12px] text-muted-foreground">
                No suitable lead?{' '}
                <Link to="/tm/leads/create" className="text-primary hover:underline" onClick={() => setPickerOpen(false)}>Create a new lead</Link>
                {' '}first — every lead has an invoice.
              </div>
            </div>
            <div className="flex-1 overflow-y-auto">
              {pickerResults.length === 0 && (
                <div className="px-4 py-6 text-center text-[13px] text-muted-foreground">No matching leads.</div>
              )}
              {pickerResults.map((l) => (
                <button
                  key={l.id}
                  type="button"
                  onClick={() => { setPickerOpen(false); navigate(`/tm/leads/${l.id}/invoice`); }}
                  className="w-full text-left px-4 py-2.5 border-b border-border hover:bg-muted flex items-center justify-between gap-3"
                >
                  <div className="min-w-0">
                    <div className="text-[13.5px] font-medium truncate">{l.business_name || l.name}</div>
                    <div className="text-[12px] text-muted-foreground truncate">
                      {l.proposed_trademark || '—'} · {l.mobile || 'no mobile'}
                    </div>
                  </div>
                  <span className={`pill ${paymentStatusClass(l.payment_status)} flex-shrink-0`}><span className="dot" />{l.payment_status}</span>
                </button>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
