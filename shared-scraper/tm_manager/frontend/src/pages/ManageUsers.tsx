import { useCallback, useEffect, useState } from 'react';
import { Plus, Search, Shield, ShieldOff, UserX, UserCheck, Pencil, X, Check } from 'lucide-react';
import Ring from '../components/tm/Ring';
import { API_BASE } from '../lib/api';
import { useAuth } from '../context/AuthContext';

interface ManagedUser {
  id: number;
  email: string;
  display_name: string | null;
  is_admin: boolean;
  is_active: boolean;
  created_at: string;
  last_login_at: string | null;
  trademark_count: number;
}

export default function ManageUsers() {
  const { user: me } = useAuth();
  const [users, setUsers] = useState<ManagedUser[] | null>(null);
  const [error, setError] = useState('');

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [isAdmin, setIsAdmin] = useState(false);
  const [creating, setCreating] = useState(false);
  const [formError, setFormError] = useState('');

  const [query, setQuery] = useState('');
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editEmail, setEditEmail] = useState('');
  const [editName, setEditName] = useState('');
  const [editPassword, setEditPassword] = useState('');
  const [saving, setSaving] = useState(false);
  const [editError, setEditError] = useState('');

  const load = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/users`);
      const json = await res.json();
      if (!res.ok) {
        setError(json.error || 'Could not load users.');
        return;
      }
      setUsers(json.users);
    } catch {
      setError('Could not reach the server.');
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const createUser = async (e: React.FormEvent) => {
    e.preventDefault();
    setCreating(true);
    setFormError('');
    try {
      const res = await fetch(`${API_BASE}/api/users`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password, display_name: displayName, is_admin: isAdmin }),
      });
      const json = await res.json();
      if (!res.ok) {
        setFormError(json.error || 'Could not create user.');
        return;
      }
      setEmail('');
      setPassword('');
      setDisplayName('');
      setIsAdmin(false);
      load();
    } catch {
      setFormError('Could not reach the server.');
    } finally {
      setCreating(false);
    }
  };

  const toggleActive = async (u: ManagedUser) => {
    await fetch(`${API_BASE}/api/users/${u.id}/active`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ is_active: !u.is_active }),
    });
    load();
  };

  const toggleAdmin = async (u: ManagedUser) => {
    await fetch(`${API_BASE}/api/users/${u.id}/admin`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ is_admin: !u.is_admin }),
    });
    load();
  };

  const startEdit = (u: ManagedUser) => {
    setEditingId(u.id);
    setEditEmail(u.email);
    setEditName(u.display_name || '');
    setEditPassword('');
    setEditError('');
  };

  const cancelEdit = () => {
    setEditingId(null);
    setEditError('');
  };

  const saveEdit = async (u: ManagedUser) => {
    setSaving(true);
    setEditError('');
    try {
      const res = await fetch(`${API_BASE}/api/users/${u.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: editEmail, display_name: editName }),
      });
      const json = await res.json();
      if (!res.ok) {
        setEditError(json.error || 'Could not save changes.');
        return;
      }
      // Password is a separate call — only sent if the admin actually typed
      // one, so leaving it blank never accidentally resets it.
      if (editPassword) {
        if (editPassword.length < 8) {
          setEditError('New password must be at least 8 characters.');
          return;
        }
        const pwRes = await fetch(`${API_BASE}/api/users/${u.id}/password`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ password: editPassword }),
        });
        const pwJson = await pwRes.json();
        if (!pwRes.ok) {
          setEditError(pwJson.error || 'Details saved, but the password could not be updated.');
          return;
        }
      }
      setEditingId(null);
      load();
    } catch {
      setEditError('Could not reach the server.');
    } finally {
      setSaving(false);
    }
  };

  if (error) {
    return (
      <div className="flex-1 min-h-0 overflow-y-auto px-7.5 pt-6 pb-15">
        <div className="max-w-160 rounded-lg px-4 py-3 text-[12.5px] bg-status-bad-bg text-status-bad">{error}</div>
      </div>
    );
  }

  if (!users) return <div className="loading"><Ring state="loading" size={28} /><span>Loading users…</span></div>;

  const needle = query.trim().toLowerCase();
  const shown = needle
    ? users.filter((u) => `${u.display_name || ''} ${u.email}`.toLowerCase().includes(needle))
    : users;

  return (
    <div className="flex-1 min-h-0 overflow-y-auto px-7.5 pt-6 pb-15">
      <div className="mb-5">
        <h1 className="text-[19px] font-semibold m-0">Manage users</h1>
        <p className="text-muted-foreground text-[13px] mt-1">
          Sign-up is invite-only — this is the only way new people get access to TM Tracker.
        </p>
      </div>

      <form onSubmit={createUser} className="max-w-160 bg-panel border border-border rounded-xl p-5 mb-6">
        <div className="eyebrow text-muted-foreground mb-3">Add a new user</div>
        {formError && (
          <div className="rounded-lg px-3.5 py-2.5 text-[12.5px] mb-3 bg-status-bad-bg text-status-bad">{formError}</div>
        )}
        <div className="grid grid-cols-2 gap-3 mb-3">
          <div>
            <label className="block eyebrow text-muted-foreground! mb-1.5" htmlFor="new-email">Email</label>
            <input
              id="new-email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              className="w-full border border-border-strong rounded-md px-2.5 py-2 text-[13px] outline-none focus:border-primary"
            />
          </div>
          <div>
            <label className="block eyebrow text-muted-foreground! mb-1.5" htmlFor="new-name">Display name (optional)</label>
            <input
              id="new-name"
              type="text"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              className="w-full border border-border-strong rounded-md px-2.5 py-2 text-[13px] outline-none focus:border-primary"
            />
          </div>
        </div>
        <div className="mb-3">
          <label className="block eyebrow text-muted-foreground! mb-1.5" htmlFor="new-password">Password</label>
          <input
            id="new-password"
            type="text"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            minLength={8}
            placeholder="At least 8 characters"
            className="w-full max-w-80 border border-border-strong rounded-md px-2.5 py-2 text-[13px] outline-none focus:border-primary"
          />
        </div>
        <label className="flex items-center gap-2 text-[12.5px] text-muted-foreground mb-4">
          <input type="checkbox" checked={isAdmin} onChange={(e) => setIsAdmin(e.target.checked)} />
          Make this an admin account (sees every user's trademarks)
        </label>
        <button type="submit" disabled={creating} className="btn btn-accent inline-flex items-center gap-1.5">
          <Plus size={14} /> {creating ? 'Creating…' : 'Create user'}
        </button>
      </form>

      <div className="max-w-160 mb-3 flex items-center gap-3">
        <div className="relative flex-1">
          <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground pointer-events-none" />
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search users by name or email…"
            aria-label="Search users"
            className="w-full border border-border-strong rounded-md pl-8 pr-2.5 py-2 text-[13px] outline-none focus:border-primary bg-background"
          />
        </div>
        <span className="text-[12px] text-muted-foreground whitespace-nowrap">
          {needle ? `${shown.length} of ${users.length} users` : `${users.length} user${users.length === 1 ? '' : 's'}`}
        </span>
      </div>

      <div className="max-w-160 border border-border rounded-xl overflow-hidden">
        {shown.length === 0 && (
          <div className="px-4 py-6 text-center text-[13px] text-muted-foreground">No users match “{query.trim()}”.</div>
        )}
        {shown.map((u) => (
          editingId === u.id ? (
            <div key={u.id} className="px-4 py-3.5 border-b border-border last:border-b-0 bg-muted/40">
              {editError && (
                <div className="rounded-lg px-3 py-2 text-[12px] mb-2.5 bg-status-bad-bg text-status-bad">{editError}</div>
              )}
              <div className="grid grid-cols-2 gap-2.5 mb-2.5">
                <div>
                  <label className="block eyebrow text-muted-foreground! mb-1" htmlFor={`edit-name-${u.id}`}>Display name</label>
                  <input
                    id={`edit-name-${u.id}`}
                    type="text"
                    value={editName}
                    onChange={(e) => setEditName(e.target.value)}
                    className="w-full border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary"
                  />
                </div>
                <div>
                  <label className="block eyebrow text-muted-foreground! mb-1" htmlFor={`edit-email-${u.id}`}>Email</label>
                  <input
                    id={`edit-email-${u.id}`}
                    type="email"
                    value={editEmail}
                    onChange={(e) => setEditEmail(e.target.value)}
                    className="w-full border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary"
                  />
                </div>
              </div>
              <div className="mb-3">
                <label className="block eyebrow text-muted-foreground! mb-1" htmlFor={`edit-pw-${u.id}`}>New password (leave blank to keep current)</label>
                <input
                  id={`edit-pw-${u.id}`}
                  type="text"
                  value={editPassword}
                  onChange={(e) => setEditPassword(e.target.value)}
                  placeholder="At least 8 characters"
                  className="w-full max-w-80 border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary"
                />
              </div>
              <div className="flex items-center gap-2">
                <button type="button" onClick={() => saveEdit(u)} disabled={saving} className="btn btn-accent inline-flex items-center gap-1.5 text-[12.5px] px-2.5 py-1.5">
                  <Check size={13} /> {saving ? 'Saving…' : 'Save'}
                </button>
                <button type="button" onClick={cancelEdit} disabled={saving} className="btn-ghost inline-flex items-center gap-1.5 text-[12.5px] px-2.5 py-1.5 rounded-md">
                  <X size={13} /> Cancel
                </button>
              </div>
            </div>
          ) : (
            <div key={u.id} className="flex items-center gap-3 px-4 py-3 border-b border-border last:border-b-0">
              <div className="min-w-0 flex-1">
                <div className="text-[13px] font-medium truncate flex items-center gap-1.5">
                  {u.display_name || u.email}
                  {u.is_admin && <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-status-good-bg text-status-good">admin</span>}
                  {!u.is_active && <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-status-bad-bg text-status-bad">deactivated</span>}
                </div>
                <div className="text-[11.5px] text-muted-foreground truncate">
                  {u.email} · {u.trademark_count} trademark{u.trademark_count === 1 ? '' : 's'}
                </div>
              </div>
              <div className="flex items-center gap-1.5 flex-shrink-0">
                <button
                  type="button"
                  onClick={() => startEdit(u)}
                  title="Edit name, email or password"
                  className="btn-ghost p-1.5 rounded-md"
                >
                  <Pencil size={15} />
                </button>
                {u.id !== me?.id && (
                  <>
                    <button
                      type="button"
                      onClick={() => toggleAdmin(u)}
                      title={u.is_admin ? 'Revoke admin' : 'Make admin'}
                      className="btn-ghost p-1.5 rounded-md"
                    >
                      {u.is_admin ? <ShieldOff size={15} /> : <Shield size={15} />}
                    </button>
                    <button
                      type="button"
                      onClick={() => toggleActive(u)}
                      title={u.is_active ? 'Deactivate' : 'Reactivate'}
                      className="btn-ghost p-1.5 rounded-md"
                    >
                      {u.is_active ? <UserX size={15} /> : <UserCheck size={15} />}
                    </button>
                  </>
                )}
              </div>
            </div>
          )
        ))}
      </div>
    </div>
  );
}
