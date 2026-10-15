// Thin fetch wrapper for the /api/tm/* Lead & Invoice endpoints — same
// API_BASE (port 5005) as the rest of this app, just namespaced under its
// own path prefix on the backend.
import { API_BASE } from './api';

const BASE = `${API_BASE}/api/tm`;

async function request(path: string, options?: RequestInit) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    ...options,
  });
  if (res.status === 401) {
    // session expired / not logged in -> bounce to the login page
    window.location.href = `/login?next=${encodeURIComponent(window.location.pathname)}`;
    throw new Error('Not authenticated');
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || `Request failed: ${res.status}`);
  }
  return res.json();
}

export const tmApi = {
  // Leads
  listLeads: (params: Record<string, string> = {}) =>
    request(`/leads?${new URLSearchParams(params).toString()}`),
  createLead: (data: any) => request('/leads', { method: 'POST', body: JSON.stringify(data) }),
  getLead: (id: number | string) => request(`/leads/${id}`),
  updateLead: (id: number | string, data: any) => request(`/leads/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteLead: (id: number | string) => request(`/leads/${id}`, { method: 'DELETE' }),
  kanban: () => request('/leads/kanban'),
  kanbanUpdate: (lead_id: number, stage: string) =>
    request('/leads/kanban/update', { method: 'POST', body: JSON.stringify({ lead_id, stage }) }),
  invoicesList: (params: Record<string, string> = {}) =>
    request(`/leads/invoices?${new URLSearchParams(params).toString()}`),
  globalSearch: (q: string) => request(`/leads/global-search?q=${encodeURIComponent(q)}`),

  // Sub-actions
  qualify: (id: number | string, data: any) => request(`/leads/${id}/qualify`, { method: 'POST', body: JSON.stringify(data) }),
  mapConsultant: (id: number | string, data: any) => request(`/leads/${id}/map-consultant`, { method: 'POST', body: JSON.stringify(data) }),
  updatePayment: (id: number | string, data: any) => request(`/leads/${id}/update-payment`, { method: 'POST', body: JSON.stringify(data) }),
  updateWorkflow: (id: number | string, data: any) => request(`/leads/${id}/update-workflow`, { method: 'POST', body: JSON.stringify(data) }),
  updateTracking: (id: number | string, data: any) => request(`/leads/${id}/update-tracking`, { method: 'POST', body: JSON.stringify(data) }),
  updateObjection: (id: number | string, data: any) => request(`/leads/${id}/update-objection`, { method: 'POST', body: JSON.stringify(data) }),
  updatePublication: (id: number | string, data: any) => request(`/leads/${id}/update-publication`, { method: 'POST', body: JSON.stringify(data) }),
  updateRegistration: (id: number | string, data: any) => request(`/leads/${id}/update-registration`, { method: 'POST', body: JSON.stringify(data) }),
  updateInvoice: (id: number | string, data: any) => request(`/leads/${id}/update-invoice`, { method: 'POST', body: JSON.stringify(data) }),

  // Consultants
  listConsultants: () => request('/consultants'),
  createConsultant: (data: any) => request('/consultants', { method: 'POST', body: JSON.stringify(data) }),
  updateConsultant: (id: string, data: any) => request(`/consultants/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteConsultant: (id: string) => request(`/consultants/${id}`, { method: 'DELETE' }),

  // Packages
  listPackages: () => request('/packages'),
  createPackage: (data: any) => request('/packages', { method: 'POST', body: JSON.stringify(data) }),
  updatePackage: (id: string, data: any) => request(`/packages/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deletePackage: (id: string) => request(`/packages/${id}`, { method: 'DELETE' }),

  // Settings
  getSettings: () => request('/settings'),
  updateSettings: (data: any) => request('/settings', { method: 'POST', body: JSON.stringify(data) }),

  // Dashboard
  dashboard: () => request('/dashboard'),
};
