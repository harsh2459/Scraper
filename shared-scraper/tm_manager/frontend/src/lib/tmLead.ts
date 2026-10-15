// Shared lead/invoice helpers — kept out of individual pages since the
// Lead Detail, Invoice View and All Invoices pages all need the same
// total-calculation and status-color rules.

export function invoiceItems(lead: any): { description: string; amount: number }[] {
  if (!lead?.invoice_items) return [];
  const parsed = typeof lead.invoice_items === 'string' ? JSON.parse(lead.invoice_items) : lead.invoice_items;
  return Array.isArray(parsed) ? parsed : [];
}

export function invoiceTotal(lead: any): number {
  const base = Number(lead?.pricing || 0);
  const extra = invoiceItems(lead).reduce((sum, item) => sum + Number(item.amount || 0), 0);
  return base + extra;
}

export function invoiceNumber(lead: any): string {
  const year = lead?.created_at ? new Date(lead.created_at).getFullYear() : new Date().getFullYear();
  return `#INV-${year}-${String(lead?.id ?? '').padStart(4, '0')}`;
}

const KANBAN_STAGE_CLASS: Record<string, string> = {
  'New Leads': 's-info',
  Verified: 's-info',
  Consulting: 's-info',
  'Payment Done': 's-good',
  'Docs Ready': 's-good',
  'App Filed': 's-info',
  'Recently Applied': 's-info',
  'Marked for Exam': 's-info',
  'Formality Pass': 's-good',
  'Formality Fail': 's-warn',
  'Objection/Opp.': 's-bad',
  'Reply Filed': 's-info',
  Advertised: 's-info',
  Hearing: 's-warn',
  'TM-M Filed': 's-info',
  'Exam Pending': 's-info',
  'Exam Report': 's-info',
  Registered: 's-good',
};

export function kanbanStageClass(stage: string): string {
  return KANBAN_STAGE_CLASS[stage] || 's-info';
}

export function paymentStatusClass(status: string): string {
  return status === 'Payment Done' ? 's-good' : 's-warn';
}

export const KANBAN_STAGES = [
  'New Leads', 'Verified', 'Consulting', 'Payment Done', 'Docs Ready', 'App Filed',
  'Recently Applied', 'Marked for Exam', 'Formality Pass', 'Formality Fail',
  'Objection/Opp.', 'Reply Filed', 'Advertised', 'Hearing', 'TM-M Filed',
  'Exam Pending', 'Exam Report', 'Registered',
];

export const WORKFLOW_STEPS: { key: string; label: string }[] = [
  { key: 'partnership_deed', label: 'Partnership Deed' },
  { key: 'firm_pan', label: 'Firm PAN' },
  { key: 'partner_kyc', label: 'Partner KYC' },
  { key: 'udyam_reg', label: 'Udyam Registration' },
  { key: 'brand_logo', label: 'Brand Logo' },
  { key: 'firm_email_verified', label: 'Firm Email Verified' },
  { key: 'poa_drafted', label: 'POA Drafted' },
  { key: 'stamp_purchased', label: 'Stamp Purchased' },
  { key: 'poa_signed', label: 'POA Signed' },
  { key: 'tma_drafted', label: 'TMA Drafted' },
  { key: 'tma_shared', label: 'TMA Shared' },
  { key: 'invoice_generated', label: 'Invoice Generated' },
  { key: 'final_payment', label: 'Final Payment' },
];

export function formatCurrency(n: number): string {
  return `₹${Number(n || 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' });
}
