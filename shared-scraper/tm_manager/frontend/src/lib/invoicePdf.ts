// One-click PDF export for an invoice — used by both the Invoices list
// (no navigation) and the Invoice detail page's own Download button.
// Renders a plain, static HTML string off-screen (not the live React tree,
// which has editable <input>s baked into it) and hands that node to
// html2pdf, which rasterizes it via html2canvas and wraps it in a jsPDF.
import html2pdf from 'html2pdf.js';
import { invoiceItems, invoiceTotal, invoiceNumber, formatCurrency, formatDate } from './tmLead';

function escapeHtml(value: unknown): string {
  return String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c] as string
  ));
}

function buildInvoiceHtml(lead: any, settings: any): string {
  const items = invoiceItems(lead);
  const total = invoiceTotal(lead);
  const packageName = lead.invoice_package_name || 'TRADEMARK APPLICATION';
  const packageDesc = lead.invoice_package_description || 'TM APPLICATION FEES';

  const itemRows = items.map((item) => `
    <tr style="border-bottom:1px solid #E4E8F1;">
      <td style="padding:8px 0;font-size:13px;">${escapeHtml(item.description)}</td>
      <td style="padding:8px 0;font-size:13px;text-align:right;font-family:monospace;">${escapeHtml(formatCurrency(item.amount))}</td>
    </tr>`).join('');

  return `
  <div style="font-family:'Inter',Arial,Helvetica,sans-serif;color:#0C1530;padding:32px;width:696px;background:#fff;">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:32px;">
      <div style="max-width:420px;">
        ${settings.company_logo ? `<img src="${settings.company_logo}" style="height:48px;max-width:160px;object-fit:contain;margin-bottom:8px;" />` : ''}
        <div style="font-size:18px;font-weight:700;">${escapeHtml(settings.company_name || 'Your Company Name')}</div>
        <div style="font-size:12px;color:#5A6683;margin-top:4px;">${escapeHtml(settings.company_address_1)}</div>
        <div style="font-size:12px;color:#5A6683;">${escapeHtml(settings.company_address_2)}</div>
        <div style="font-size:12px;color:#5A6683;">${escapeHtml(settings.company_city)}</div>
        <div style="font-size:12px;color:#5A6683;">${escapeHtml(settings.company_email)}</div>
      </div>
      <div style="text-align:right;">
        <div style="font-size:20px;font-weight:700;font-family:'Space Grotesk',Arial,sans-serif;color:#152449;">INVOICE</div>
        <div style="font-size:13px;font-family:monospace;margin-top:4px;">${escapeHtml(invoiceNumber(lead))}</div>
        <div style="font-size:12px;color:#5A6683;margin-top:4px;">${escapeHtml(formatDate(lead.created_at))}</div>
      </div>
    </div>

    <div style="display:flex;justify-content:space-between;gap:16px;margin-bottom:32px;padding-bottom:24px;border-bottom:1px solid #E4E8F1;">
      <div>
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.05em;color:#5A6683;margin-bottom:4px;">Billed To</div>
        <div style="font-size:14px;font-weight:600;">${escapeHtml(lead.business_name || lead.name)}</div>
        <div style="font-size:12.5px;color:#5A6683;">${escapeHtml(lead.name)}</div>
        <div style="font-size:12.5px;color:#5A6683;font-family:monospace;">${escapeHtml(lead.mobile)}</div>
      </div>
      <div>
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.05em;color:#5A6683;margin-bottom:4px;">Trademark</div>
        <div style="font-size:14px;font-weight:600;">${escapeHtml(lead.proposed_trademark || '—')}</div>
        <div style="font-size:12.5px;color:#5A6683;">Class ${escapeHtml(lead.class || '—')}</div>
      </div>
    </div>

    <table style="width:100%;border-collapse:collapse;margin-bottom:16px;">
      <thead>
        <tr style="border-bottom:1px solid #E4E8F1;">
          <th style="text-align:left;font-size:12px;font-weight:500;text-transform:uppercase;letter-spacing:0.05em;color:#5A6683;padding-bottom:8px;">Description</th>
          <th style="text-align:right;font-size:12px;font-weight:500;text-transform:uppercase;letter-spacing:0.05em;color:#5A6683;padding-bottom:8px;">Amount</th>
        </tr>
      </thead>
      <tbody>
        <tr style="border-bottom:1px solid #E4E8F1;">
          <td style="padding:10px 0;font-size:13.5px;">
            <div style="font-weight:600;">${escapeHtml(packageName)}</div>
            <div style="font-size:12px;color:#5A6683;">${escapeHtml(packageDesc)}</div>
          </td>
          <td style="padding:10px 0;font-size:13.5px;text-align:right;font-family:monospace;">${escapeHtml(formatCurrency(lead.pricing))}</td>
        </tr>
        ${itemRows}
      </tbody>
    </table>

    <div style="display:flex;justify-content:flex-end;margin-bottom:32px;">
      <div style="width:240px;">
        <div style="display:flex;justify-content:space-between;padding:8px 0;border-top:1px solid #E4E8F1;font-size:15px;font-weight:700;">
          <span>Total</span>
          <span style="font-family:monospace;">${escapeHtml(formatCurrency(total))}</span>
        </div>
        <div style="display:flex;justify-content:space-between;padding:4px 0;font-size:12.5px;">
          <span style="color:#5A6683;">Payment Status</span>
          <span style="font-weight:500;color:${lead.payment_status === 'Payment Done' ? '#10B981' : '#F6A21E'};">${escapeHtml(lead.payment_status)}</span>
        </div>
      </div>
    </div>

    ${lead.invoice_remarks ? `<div style="font-size:12px;color:#5A6683;">${escapeHtml(lead.invoice_remarks)}</div>` : ''}
  </div>`;
}

export async function downloadInvoicePdf(lead: any, settings: any): Promise<void> {
  const container = document.createElement('div');
  container.style.position = 'fixed';
  container.style.left = '-10000px';
  container.style.top = '0';
  container.innerHTML = buildInvoiceHtml(lead, settings);
  document.body.appendChild(container);

  try {
    await html2pdf()
      .set({
        margin: 0,
        filename: `${invoiceNumber(lead)}.pdf`,
        image: { type: 'jpeg', quality: 0.98 },
        html2canvas: { scale: 2, useCORS: true },
        jsPDF: { unit: 'pt', format: 'a4', orientation: 'portrait' },
      })
      .from(container.firstElementChild as HTMLElement)
      .save();
  } finally {
    document.body.removeChild(container);
  }
}
