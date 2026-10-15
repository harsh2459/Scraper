import { createContext, useCallback, useContext, useEffect, useState } from 'react';
import { API_BASE } from '../lib/api';

export interface AuthUser {
  id: number;
  email: string;
  display_name: string | null;
  is_admin: boolean;
  fleet_control_enabled?: boolean;
}

interface AuthContextValue {
  user: AuthUser | null;
  isLoading: boolean;
  login: (email: string, password: string, remember: boolean) => Promise<{ ok: boolean; error?: string }>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

// The real security boundary is server-side (_require_login in app.py
// redirects/401s before this ever runs) — this context is purely for UI
// state (what to render, who to show as logged in), not the boundary
// itself.
export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/me`);
      const json = await res.json();
      setUser(json.auth ? json.user : null);
    } catch {
      setUser(null);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const login = useCallback(async (email: string, password: string, remember: boolean) => {
    try {
      const res = await fetch(`${API_BASE}/api/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password, remember }),
      });
      const json = await res.json();
      if (json.ok) {
        setUser(json.user);
        return { ok: true };
      }
      return { ok: false, error: json.error || 'Invalid email or password.' };
    } catch {
      return { ok: false, error: 'Could not reach the server.' };
    }
  }, []);

  const logout = useCallback(async () => {
    try {
      await fetch(`${API_BASE}/api/logout`, { method: 'POST' });
    } catch {
      // ignore — clearing local state below is what actually matters for the UI
    }
    setUser(null);
  }, []);

  return (
    <AuthContext.Provider value={{ user, isLoading, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within an AuthProvider');
  return ctx;
}
