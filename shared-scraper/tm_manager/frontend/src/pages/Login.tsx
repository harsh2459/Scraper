import { useState } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';

export default function Login() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [remember, setRemember] = useState(true);
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError('');
    const result = await login(email, password, remember);
    setSubmitting(false);
    if (result.ok) {
      const next = (location.state as { next?: string } | null)?.next || '/';
      navigate(next, { replace: true });
    } else {
      setError(result.error || 'Invalid email or password.');
    }
  };

  return (
    <div className="min-h-screen w-full flex items-center justify-center bg-background px-4">
      <form onSubmit={submit} className="w-full max-w-90 bg-surface border border-border rounded-2xl p-8.5 shadow-sm">
        <div className="flex items-center gap-2.5 mb-6">
          <div className="w-7 h-7 rounded-lg bg-accent flex items-center justify-center text-accent-foreground font-bold text-sm flex-shrink-0">T</div>
          <span className="font-display text-[17px] font-bold tracking-tight">
            TM <span className="font-medium">Tracker</span>
            <sup className="text-accent text-[10px] font-bold ml-0.5">&#8482;</sup>
          </span>
        </div>

        <h1 className="text-[19px] font-semibold m-0 mb-1">Sign in</h1>
        <p className="text-[13px] text-muted-foreground mb-5">Enter your account details to continue.</p>

        {error && (
          <div className="rounded-lg px-3.5 py-2.5 text-[12.5px] mb-4 bg-status-bad-bg text-status-bad">{error}</div>
        )}

        <label className="block eyebrow text-muted-foreground! mb-1.5" htmlFor="login-email">Email</label>
        <input
          id="login-email"
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          autoComplete="email"
          autoFocus
          required
          className="w-full border border-border-strong rounded-md px-3 py-2.5 text-[13.5px] outline-none focus:border-primary mb-4"
        />

        <label className="block eyebrow text-muted-foreground! mb-1.5" htmlFor="login-password">Password</label>
        <input
          id="login-password"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
          required
          className="w-full border border-border-strong rounded-md px-3 py-2.5 text-[13.5px] outline-none focus:border-primary"
        />

        <label className="flex items-center gap-2 mt-4 text-[12.5px] text-muted-foreground">
          <input
            type="checkbox"
            id="login-remember"
            checked={remember}
            onChange={(e) => setRemember(e.target.checked)}
          />
          Keep me signed in for 30 days
        </label>

        <button type="submit" disabled={submitting} className="btn btn-accent w-full mt-5">
          {submitting ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
      <p className="mt-4 text-center text-[12px] text-muted-foreground"><a className="hover:underline" href="/privacy.html">Privacy Policy</a> · <a className="hover:underline" href="/terms.html">Terms of Service</a></p>
    </div>
  );
}
