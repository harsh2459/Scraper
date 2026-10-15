import { useEffect, useRef, useState } from 'react';
import Ring from '../../components/tm/Ring';
import { useNavigate, useParams, useSearchParams, Link } from 'react-router-dom';
import { ArrowLeft, Plus, Trash2, Printer, Download, Settings as SettingsIcon } from 'lucide-react';
import { tmApi } from '../../lib/tmApi';
import { invoiceItems, invoiceTotal, invoiceNumber, formatCurrency, formatDate } from '../../lib/tmLead';
import { downloadInvoicePdf } from '../../lib/invoicePdf';
import { useToast } from '../../components/tm/Toast';

const inputClass = 'border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary w-full';

export default function InvoiceView() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { showToast } = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const [lead, setLead] = useState<any>(null);
  const [settings, setSettings] = useState<any>(null);
  const [items, setItems] = useState<{ description: string; amount: number }[]>([]);
  const [packageName, setPackageName] = useState('');
  const [packageDesc, setPackageDesc] = useState('');
  const [remarks, setRemarks] = useState('');
  const [saving, setSaving] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const autoPrinted = useRef(false);

  useEffect(() => {
    tmApi.getLead(id!).then((json) => {
      setLead(json.lead);
      setItems(invoiceItems(json.lead));
      setPackageName(json.lead.invoice_package_name || 'TRADEMARK APPLICATION');
      setPackageDesc(json.lead.invoice_package_description || 'TM APPLICATION FEES');
      setRemarks(json.lead.invoice_remarks || '');
    }).catch((err) => {
      showToast('error', err.message || 'Lead not found');
      navigate('/tm/leads');
    });
    tmApi.getSettings().then(setSettings).catch(() => {});
  }, [id]);

  // Arriving from the Invoices list "Print"/"Download" buttons carries
  // ?print=1 so the dialog opens as soon as the invoice has actually
  // rendered, instead of the list trying to print itself.
  useEffect(() => {
    if (lead && settings && searchParams.get('print') === '1' && !autoPrinted.current) {
      autoPrinted.current = true;
      setSearchParams((p) => { p.delete('print'); return p; }, { replace: true });
      setTimeout(() => window.print(), 300);
    }
  }, [lead, settings, searchParams, setSearchParams]);

  if (!lead || !settings) return <div className="loading"><Ring state="loading" size={28} /><span>Loading invoice…</span></div>;

  const total = invoiceTotal({ ...lead, invoice_items: items });

  const save = async () => {
    setSaving(true);
    try {
      const updated = await tmApi.updateInvoice(id!, {
        invoice_package_name: packageName,
        invoice_package_description: packageDesc,
        invoice_items: items,
        invoice_remarks: remarks,
      });
      setLead(updated);
      showToast('success', 'Invoice saved.');
    } catch (err: any) {
      showToast('error', err.message || 'Save failed');
    } finally {
      setSaving(false);
    }
  };

  // Downloads whatever is currently on screen — including unsaved edits to
  // line items/remarks — same as what "Save Invoice" would persist, since
  // the PDF is a snapshot of this preview, not a re-fetch from the server.
  const download = async () => {
    setDownloading(true);
    try {
      await downloadInvoicePdf(
        { ...lead, invoice_package_name: packageName, invoice_package_description: packageDesc, invoice_items: items, invoice_remarks: remarks },
        settings,
      );
    } catch (err: any) {
      showToast('error', err.message || 'Failed to generate PDF');
    } finally {
      setDownloading(false);
    }
  };

  return (
    <div className="flex-1 min-h-0 flex flex-col overflow-hidden print:overflow-visible print:h-auto print:block">
      <div className="px-5 pt-4 pb-2 flex-shrink-0 flex items-center justify-between print:hidden">
        <button type="button" onClick={() => navigate(`/tm/leads/${id}`)} className="flex items-center gap-1.5 text-[13px] text-muted-foreground hover:text-foreground">
          <ArrowLeft size={14} /> Back to Lead
        </button>
        <div className="flex items-center gap-2">
          <Link to="/tm/settings" className="btn" title="Edit company details"><SettingsIcon size={13} /> Company Details</Link>
          <button type="button" className="btn btn-accent" disabled={saving} onClick={save}>{saving ? 'Saving…' : 'Save Invoice'}</button>
          <button type="button" className="btn" onClick={() => window.print()}><Printer size={13} /> Print</button>
          <button type="button" className="btn" disabled={downloading} onClick={download}><Download size={13} /> {downloading ? 'Preparing…' : 'Download'}</button>
        </div>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto px-5 pb-15 print:overflow-visible print:h-auto print:block print:p-0">
        <div className="max-w-[760px] mx-auto bg-panel border border-border rounded-lg p-8 mt-2 print:border-0 print:shadow-none print:mt-0 print:max-w-none">
          <div className="flex justify-between items-start mb-8 gap-6">
            <div className="flex-1 min-w-0">
              {settings.company_logo && <img src={settings.company_logo} alt="" className="h-12 max-w-[160px] object-contain mb-2" />}
              <div className="text-[18px] font-bold">{settings.company_name || 'Your Company Name'}</div>
              <div className="text-[12px] text-muted-foreground mt-1">{settings.company_address_1}</div>
              <div className="text-[12px] text-muted-foreground">{settings.company_address_2}</div>
              <div className="text-[12px] text-muted-foreground">{settings.company_city}</div>
              <div className="text-[12px] text-muted-foreground">{settings.company_email}</div>
            </div>
            <div className="text-right flex-shrink-0">
              <div className="text-[20px] font-bold text-primary">INVOICE</div>
              <div className="text-[13px] font-mono mt-1">{invoiceNumber(lead)}</div>
              <div className="text-[12px] text-muted-foreground mt-1">{formatDate(lead.created_at)}</div>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4 mb-8 pb-6 border-b border-border">
            <div>
              <div className="text-[11px] uppercase tracking-wide text-muted-foreground mb-1">Billed To</div>
              <div className="text-[14px] font-semibold">{lead.business_name || lead.name}</div>
              <div className="text-[12.5px] text-muted-foreground">{lead.name}</div>
              <div className="text-[12.5px] text-muted-foreground font-mono">{lead.mobile}</div>
            </div>
            <div>
              <div className="text-[11px] uppercase tracking-wide text-muted-foreground mb-1">Trademark</div>
              <div className="text-[14px] font-semibold">{lead.proposed_trademark || '—'}</div>
              <div className="text-[12.5px] text-muted-foreground">Class {lead.class || '—'}</div>
            </div>
          </div>

          <div className="mb-6 print:hidden">
            <div className="grid grid-cols-2 gap-3 mb-3">
              <input className={inputClass} value={packageName} onChange={(e) => setPackageName(e.target.value)} placeholder="Package name" />
              <input className={inputClass} value={packageDesc} onChange={(e) => setPackageDesc(e.target.value)} placeholder="Package description" />
            </div>
          </div>

          <table className="w-full border-collapse mb-4">
            <thead>
              <tr className="border-b border-border">
                <th className="text-left text-[12px] font-medium py-2 uppercase tracking-wide text-muted-foreground">Description</th>
                <th className="text-right text-[12px] font-medium py-2 uppercase tracking-wide text-muted-foreground">Amount</th>
                <th className="w-8 print:hidden" />
              </tr>
            </thead>
            <tbody>
              <tr className="border-b border-border">
                <td className="py-2.5 text-[13.5px]">
                  <div className="font-medium">{packageName}</div>
                  <div className="text-[12px] text-muted-foreground">{packageDesc}</div>
                </td>
                <td className="py-2.5 text-[13.5px] text-right font-mono">{formatCurrency(lead.pricing)}</td>
                <td />
              </tr>
              {items.map((item, i) => (
                <tr key={i} className="border-b border-border">
                  <td className="py-2 text-[13px] print:hidden">
                    <input className={inputClass} value={item.description} onChange={(e) => setItems((its) => its.map((it, j) => j === i ? { ...it, description: e.target.value } : it))} />
                  </td>
                  <td className="py-2 text-[13px] hidden print:table-cell">{item.description}</td>
                  <td className="py-2 text-[13px] text-right print:hidden">
                    <input type="number" className={`${inputClass} text-right`} value={item.amount} onChange={(e) => setItems((its) => its.map((it, j) => j === i ? { ...it, amount: Number(e.target.value) } : it))} />
                  </td>
                  <td className="py-2 text-[13px] text-right font-mono hidden print:table-cell">{formatCurrency(item.amount)}</td>
                  <td className="print:hidden">
                    <button type="button" onClick={() => setItems((its) => its.filter((_, j) => j !== i))} className="text-muted-foreground hover:text-status-bad">
                      <Trash2 size={14} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <button
            type="button"
            className="btn text-[12px] mb-6 print:hidden"
            onClick={() => setItems((its) => [...its, { description: '', amount: 0 }])}
          >
            <Plus size={12} /> Add line item
          </button>

          <div className="flex justify-end mb-8">
            <div className="w-[240px]">
              <div className="flex justify-between py-2 border-t border-border text-[15px] font-bold">
                <span>Total</span>
                <span className="font-mono">{formatCurrency(total)}</span>
              </div>
              <div className="flex justify-between py-1 text-[12.5px]">
                <span className="text-muted-foreground">Payment Status</span>
                <span className={lead.payment_status === 'Payment Done' ? 'text-status-good font-medium' : 'text-status-warn font-medium'}>{lead.payment_status}</span>
              </div>
            </div>
          </div>

          <div className="print:hidden">
            <div className="eyebrow text-muted-foreground! mb-1.5">Remarks</div>
            <textarea className={inputClass} rows={2} value={remarks} onChange={(e) => setRemarks(e.target.value)} />
          </div>
          {remarks && <div className="hidden print:block text-[12px] text-muted-foreground mt-4">{remarks}</div>}
        </div>
      </div>
    </div>
  );
}
