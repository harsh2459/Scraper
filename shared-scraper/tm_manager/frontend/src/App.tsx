import { useState } from 'react';
import { BrowserRouter as Router, Routes, Route, Link, Navigate, useLocation, useNavigate } from 'react-router-dom';
import { Briefcase, FileText, LayoutDashboard, Clock, Eye, ChevronDown, ChevronsLeft, ChevronsRight, Hash, Plus, CalendarClock, Bell, ScanSearch, Search, LogOut, ShieldCheck, ShieldAlert, Server, CreditCard } from 'lucide-react';
import Login from './pages/Login';
import ManageUsers from './pages/ManageUsers';
import ManagePlans from './pages/ManagePlans';
import ManageSubscriptions from './pages/ManageSubscriptions';
import Dashboard from './pages/Dashboard';
import Trademarks from './pages/Trademarks';
import Oppositions from './pages/Oppositions';
import TrademarkDetail from './pages/TrademarkDetail';
import Tasks from './pages/Tasks';
import StatusUpdates from './pages/StatusUpdates';
import NewDocuments from './pages/NewDocuments';
import JournalWatch from './pages/JournalWatch';
import AppNoSearch from './pages/AppNoSearch';
import WordMarkSearch from './pages/WordMarkSearch';
import Placeholder from './pages/Placeholder';
import AddTrademarks from './pages/AddTrademarks';
import Fleet from './pages/Fleet';

import { ToastProvider } from './components/tm/Toast';
import { SearchJobsProvider, useSearchJobs } from './components/tm/SearchJobs';
import { ConfirmProvider } from './components/tm/ConfirmDialog';
import Ring from './components/tm/Ring';
import { AuthProvider, useAuth } from './context/AuthContext';
import './index.css';

function NavItem({ to, icon, children, active, sub = false, collapsed = false }: { to: string; icon: React.ReactNode; children: React.ReactNode; active: boolean; sub?: boolean; collapsed?: boolean }) {
  return (
    <Link
      to={to}
      title={collapsed ? (children as string) : undefined}
      className={`flex items-center gap-2 py-2 rounded-md text-[13.5px] ${collapsed ? 'justify-center px-2.5' : sub ? 'pl-8 pr-2.5' : 'px-2.5'} ${active ? 'bg-sidebar-active font-semibold text-white' : 'text-sidebar-foreground hover:bg-sidebar-hover'}`}
    >
      <span className={`w-4 h-4 flex items-center justify-center flex-shrink-0 ${active ? 'opacity-100' : 'opacity-60'}`}>{icon}</span>
      {!collapsed && <span>{children}</span>}
    </Link>
  );
}

function NavGroup({ icon, label, children, defaultOpen = true, collapsed = false }: { icon: React.ReactNode; label: string; children: React.ReactNode; defaultOpen?: boolean; collapsed?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  // Collapsed sidebar has no room for a sub-item list — just show the
  // group's own icon as a plain nav-less affordance (its children remain
  // reachable via the page routes themselves / expanding the sidebar
  // again), rather than trying to fit a flyout submenu into this pass.
  if (collapsed) {
    return (
      <div className="flex items-center justify-center py-2 rounded-md text-sidebar-foreground/60" title={label}>
        <span className="w-4 h-4 flex items-center justify-center flex-shrink-0">{icon}</span>
      </div>
    );
  }
  return (
    <div className="flex flex-col">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 px-2.5 py-2 rounded-md text-[13.5px] font-medium text-sidebar-foreground hover:bg-sidebar-hover w-full text-left"
      >
        <span className="w-4 h-4 flex items-center justify-center flex-shrink-0 opacity-60">{icon}</span>
        <span>{label}</span>
        <ChevronDown className={`ml-auto w-3.5 h-3.5 text-sidebar-foreground/60 transition-transform ${open ? '' : '-rotate-90'}`} />
      </button>
      {open && <div className="flex flex-col mt-0.5">{children}</div>}
    </div>
  );
}

function AppNoSearchNavItem({ active }: { active: boolean }) {
  const { activeJobs } = useSearchJobs();
  return (
    <Link
      to="/tm-search/app-no"
      className={`flex items-center gap-2 py-2 rounded-md text-[13.5px] pl-8 pr-2.5 ${active ? 'bg-sidebar-active font-semibold text-white' : 'text-sidebar-foreground hover:bg-sidebar-hover'}`}
    >
      <span className={`w-4 h-4 flex items-center justify-center flex-shrink-0 ${active ? 'opacity-100' : 'opacity-60'}`}><Hash size={14} /></span>
      <span>Application Number</span>
      {activeJobs.length > 0 && (
        <span className="ml-auto flex items-center gap-1 text-[10.5px] font-mono text-accent">
          <span className="w-1.5 h-1.5 rounded-full bg-accent animate-pulse" />
          {activeJobs.length}
        </span>
      )}
    </Link>
  );
}

const SIDEBAR_COLLAPSED_KEY = 'tm-sidebar-collapsed';

function Sidebar() {
  const location = useLocation();
  const navigate = useNavigate();
  const path = location.pathname;
  const { user, logout } = useAuth();
  const initials = (user?.display_name || user?.email || '?')
    .split(/[\s@.]+/).filter(Boolean).slice(0, 2).map((s) => s[0]?.toUpperCase()).join('') || '?';

  // Purely a per-viewer convenience (how much table/content room they want
  // right now) — localStorage is the right fit, no need for a server round
  // trip or per-user account setting for this.
  const [collapsed, setCollapsed] = useState(() => {
    try { return localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === '1'; } catch { return false; }
  });
  const toggleCollapsed = () => {
    setCollapsed((v) => {
      const next = !v;
      try { localStorage.setItem(SIDEBAR_COLLAPSED_KEY, next ? '1' : '0'); } catch { /* ignore */ }
      return next;
    });
  };

  return (
    <aside className={`print:hidden ${collapsed ? 'w-15' : 'w-60.5'} flex-shrink-0 bg-sidebar border-r border-sidebar-active px-3.5 py-5 flex flex-col gap-0.5 sticky top-0 h-screen overflow-y-auto transition-[width] duration-150`}>
      <div className={`flex items-center pb-4.5 ${collapsed ? 'justify-center px-0' : 'gap-2.5 px-1.5'}`}>
        <Ring state="brand" size={30} />
        {!collapsed && (
          <span className="font-display text-[17px] font-bold tracking-tight text-white">
            TM <span className="font-medium">Tracker</span>
            <sup className="text-accent text-[10px] font-bold ml-0.5">™</sup>
          </span>
        )}
      </div>

      <button
        type="button"
        onClick={toggleCollapsed}
        title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        className={`flex items-center gap-2 py-1.5 rounded-md text-[12px] text-sidebar-foreground/70 hover:bg-sidebar-hover hover:text-sidebar-foreground mb-2 ${collapsed ? 'justify-center px-2.5' : 'px-2.5'}`}
      >
        {collapsed ? <ChevronsRight size={14} /> : <><ChevronsLeft size={14} /> Collapse</>}
      </button>

      {!collapsed && <div className="eyebrow text-sidebar-foreground/45 mx-2 mt-2 mb-1">Workspace</div>}
      <NavItem to="/" icon={<LayoutDashboard size={15} />} active={path === '/'} collapsed={collapsed}>Dashboard</NavItem>

      <div className="mt-4">
        <NavGroup icon={<Briefcase size={15} />} label="My Portfolio" collapsed={collapsed}>
          <NavItem to="/trademarks" icon={<Hash size={14} />} active={path.startsWith('/trademarks') || path.startsWith('/trademark/')} sub>
            Trademarks
          </NavItem>
          <NavItem to="/oppositions" icon={<ShieldAlert size={14} />} active={path === '/oppositions'} sub>
            Oppositions
          </NavItem>
          <NavItem to="/add" icon={<Plus size={14} />} active={path === '/add'} sub>
            Add Trademarks
          </NavItem>
        </NavGroup>
      </div>

      {!collapsed && <div className="eyebrow text-sidebar-foreground/45 mx-2 mt-4 mb-1">Monitoring &amp; Watch</div>}
      <NavItem to="/tasks" icon={<CalendarClock size={15} />} active={path === '/tasks'} collapsed={collapsed}>Tasks &amp; Compliances</NavItem>

      <NavGroup icon={<Bell size={15} />} label="All Updates" collapsed={collapsed}>
        <NavItem to="/updates/statuses" icon={<Clock size={14} />} active={path.startsWith('/updates/statuses')} sub>
          Status Changes
        </NavItem>
        <NavItem to="/updates/documents" icon={<FileText size={14} />} active={path.startsWith('/updates/documents')} sub>
          New Documents
        </NavItem>
      </NavGroup>

      <NavGroup icon={<Eye size={15} />} label="TM Watch" collapsed={collapsed}>
        <NavItem to="/watch/journal" icon={<ScanSearch size={14} />} active={path.startsWith('/watch/journal')} sub>
          Journal Watch
        </NavItem>
      </NavGroup>

      <NavGroup icon={<Search size={15} />} label="TM Search" collapsed={collapsed}>
        <AppNoSearchNavItem active={path === '/tm-search/app-no'} />
        <NavItem to="/tm-search/word-mark" icon={<Search size={14} />} active={path === '/tm-search/word-mark'} sub>
          Word Mark
        </NavItem>
      </NavGroup>



      {user?.is_admin && user.fleet_control_enabled && (
        <>
          {!collapsed && <div className="eyebrow text-sidebar-foreground/45 mx-2 mt-4 mb-1">Admin</div>}
          <a href="/admin/" title={collapsed ? 'Administration' : undefined} className={`flex items-center gap-2 py-2 rounded-md text-[13.5px] text-sidebar-foreground hover:bg-sidebar-hover ${collapsed ? 'justify-center px-2.5' : 'px-2.5'}`}>
            <span className="w-4 h-4 flex items-center justify-center flex-shrink-0 opacity-60"><Server size={15} /></span>{!collapsed && <span>Administration</span>}
          </a>
        </>
      )}
      {user?.is_admin && !user.fleet_control_enabled && (
        <>
          {!collapsed && <div className="eyebrow text-sidebar-foreground/45 mx-2 mt-4 mb-1">Admin</div>}
          <NavItem to="/admin/users" icon={<ShieldCheck size={15} />} active={path === '/admin/users'} collapsed={collapsed}>Manage Users</NavItem>
          <NavItem to="/admin/plans" icon={<CreditCard size={15} />} active={path === '/admin/plans'} collapsed={collapsed}>Plans</NavItem>
          <NavItem to="/admin/subscriptions" icon={<CreditCard size={15} />} active={path === '/admin/subscriptions'} collapsed={collapsed}>Subscriptions</NavItem>
          <NavItem to="/admin/fleet" icon={<Server size={15} />} active={path === '/admin/fleet'} collapsed={collapsed}>Fleet</NavItem>
        </>
      )}

      <div className="mt-auto border-t border-sidebar-active pt-2">
        <div className={`flex items-center py-2 ${collapsed ? 'justify-center px-0' : 'gap-2 px-2.5'}`}>
          <div className="w-[30px] h-[30px] rounded-full bg-accent text-accent-foreground flex items-center justify-center text-[11px] font-semibold flex-shrink-0" title={collapsed ? (user?.display_name || user?.email || '') : undefined}>{initials}</div>
          {!collapsed && (
            <div className="min-w-0 flex-1">
              <div className="text-[12.5px] font-medium text-white truncate">{user?.display_name || user?.email || 'Signed in'}</div>
              <div className="text-[10.5px] text-sidebar-foreground/70 truncate">{user?.email}</div>
            </div>
          )}
        </div>
        <button
          type="button"
          onClick={async () => { await logout(); navigate('/login', { replace: true }); }}
          title={collapsed ? 'Sign out' : undefined}
          className={`flex items-center gap-2 py-2 rounded-md text-[13px] text-sidebar-foreground hover:bg-sidebar-hover ${collapsed ? 'justify-center px-2.5' : 'w-full px-2.5'}`}
        >
          <LogOut size={14} className="opacity-70" /> {!collapsed && 'Sign out'}
        </button>
      </div>
    </aside>
  );
}

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { user, isLoading } = useAuth();
  const location = useLocation();
  // The real 401/redirect boundary is server-side (app.py's _require_login)
  // — this is UX polish so a logged-out visitor sees the login page
  // immediately instead of a flash of an app shell with no data.
  if (isLoading) return null;
  if (!user) return <Navigate to="/login" replace state={{ next: location.pathname }} />;
  return <>{children}</>;
}

function AdminOnly({ children }: { children: React.ReactNode }) {
  const { user } = useAuth();
  // Same belt-and-suspenders note as RequireAuth — the real 403 boundary is
  // server-side (require_admin() in every /api/users route).
  if (!user?.is_admin) return <Navigate to="/" replace />;
  return <>{children}</>;
}

function AppShell() {
  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />
      <main className="flex-1 min-w-0 h-screen flex flex-col overflow-hidden">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/trademarks" element={<Trademarks />} />
          <Route path="/oppositions" element={<Oppositions />} />
          <Route path="/trademark/:id" element={<TrademarkDetail />} />
          <Route path="/add" element={<AddTrademarks />} />
          <Route path="/tasks" element={<Tasks />} />
          <Route path="/updates/statuses" element={<StatusUpdates />} />
          <Route path="/updates/documents" element={<NewDocuments />} />
          <Route path="/watch/journal" element={<JournalWatch />} />
          <Route path="/tm-search/app-no" element={<AppNoSearch />} />
          <Route path="/tm-search/word-mark" element={<WordMarkSearch />} />

          <Route path="/admin/users" element={<AdminOnly><ManageUsers /></AdminOnly>} />
          <Route path="/admin/plans" element={<AdminOnly><ManagePlans /></AdminOnly>} />
          <Route path="/admin/subscriptions" element={<AdminOnly><ManageSubscriptions /></AdminOnly>} />
          <Route path="/admin/fleet" element={<AdminOnly><Fleet /></AdminOnly>} />
          <Route path="*" element={<Placeholder title="Coming soon" />} />
        </Routes>
      </main>
    </div>
  );
}

function App() {
  return (
    <Router>
      <AuthProvider>
        <ToastProvider>
          <SearchJobsProvider>
            <ConfirmProvider>
              <Routes>
                <Route path="/login" element={<Login />} />
                <Route
                  path="/*"
                  element={
                    <RequireAuth>
                      <AppShell />
                    </RequireAuth>
                  }
                />
              </Routes>
            </ConfirmProvider>
          </SearchJobsProvider>
        </ToastProvider>
      </AuthProvider>
    </Router>
  );
}

export default App;
