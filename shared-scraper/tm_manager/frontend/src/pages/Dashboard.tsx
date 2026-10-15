import { useEffect, useState } from 'react';
import Ring from '../components/tm/Ring';
import { ArrowRight, FileText, CheckCircle2 } from 'lucide-react';
import { Link } from 'react-router-dom';
import { API_BASE } from '../lib/api';
import TrademarkOverlay from '../components/TrademarkOverlay';
import { useAuth } from '../context/AuthContext';

const DONUT_COLORS = [
  'var(--color-accent)', '#f59e0b', '#10b981', '#3b82f6', '#ef4444',
  '#8b5cf6', '#ec4899', '#14b8a6', '#f97316', '#6366f1',
];

function Donut({ rows, valueKey, labelKey }: { rows: any[]; valueKey: string; labelKey: string }) {
  const total = rows.reduce((sum, r) => sum + r[valueKey], 0);
  if (!total) return null;
  const size = 120;
  const r = 46;
  const c = 2 * Math.PI * r;
  let offset = 0;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="flex-shrink-0">
      <g transform={`rotate(-90 ${size / 2} ${size / 2})`}>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--color-border)" strokeWidth={14} />
        {rows.map((row, i) => {
          const frac = row[valueKey] / total;
          const dash = frac * c;
          const circle = (
            <circle
              key={row[labelKey] ?? i}
              cx={size / 2}
              cy={size / 2}
              r={r}
              fill="none"
              stroke={DONUT_COLORS[i % DONUT_COLORS.length]}
              strokeWidth={14}
              strokeDasharray={`${dash} ${c - dash}`}
              strokeDashoffset={-offset}
            />
          );
          offset += dash;
          return circle;
        })}
      </g>
      <text x="50%" y="50%" textAnchor="middle" dominantBaseline="middle" className="fill-foreground font-display font-bold" style={{ fontSize: 18 }}>
        {total}
      </text>
    </svg>
  );
}

export default function Dashboard() {
  const { user } = useAuth();
  const [data, setData] = useState<any>(null);
  const [openTmId, setOpenTmId] = useState<number | null>(null);

  useEffect(() => {
    fetch(`${API_BASE}/api/dashboard`)
      .then((res) => res.json())
      .then((json) => setData(json))
      .catch((err) => console.error(err));
  }, []);

  if (!data) return <div className="loading"><Ring state="loading" size={28} /><span>Loading dashboard…</span></div>;

  const filingCounts = data.filing_counts || [];
  const maxN = filingCounts.length ? Math.max(...filingCounts.map((c: any) => c.n)) : 0;

  // Calculate Daily Snapshot metrics
  const todayStr = new Date().toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' });
  const docsToday = (data.recent_docs || []).filter((d: any) => 
    d.date_of_document && new Date(d.date_of_document).toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' }) === todayStr
  ).length;
  const statusChangesToday = (data.status_changes || []).filter((c: any) => 
    c.changed_at && new Date(c.changed_at).toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' }) === todayStr
  ).length;
  const tasksToday = (data.recent_reminders || []).filter((r: any) =>
    (r.despatch_date || r.corres_date) && (r.despatch_date || r.corres_date).includes(new Date().toLocaleDateString('en-GB', { day: '2-digit', month: '2-digit', year: 'numeric' }))
  ).length;

  const renewalsByDate: Record<string, any[]> = data.upcoming_renewals?.by_date || {};
  const renewalDays = Array.from({ length: 7 }, (_, i) => {
    const d = new Date();
    d.setDate(d.getDate() + i);
    const key = d.toISOString().slice(0, 10);
    return {
      key,
      label: i === 0 ? 'Today' : d.toLocaleDateString('en-GB', { weekday: 'short', day: '2-digit', month: 'short' }),
    };
  });

  return (
    <div className="flex-1 min-h-0 overflow-y-auto px-7.5 pt-6 pb-15">
      <div className="mb-5">
        <h1 className="text-[22px] font-bold m-0 tracking-tight">Hello, {user?.display_name || user?.email?.split('@')[0] || 'there'}.</h1>
        <p className="text-muted-foreground text-[13px] mt-1">Portfolio, deadlines and recent movement in one view.</p>
      </div>

      <div className="grid grid-cols-[2.4fr_1fr] gap-4 items-start">
        <div>
          <div className="mb-4 flex gap-3 flex-wrap">
            <Link to="/trademarks" className="bg-panel border border-border rounded-[12px] px-4.5 py-4 flex items-center justify-between hover:border-border-strong w-fit min-w-[220px]">
              <div>
                <div className="font-display text-[26px] font-bold leading-none">{data.total ?? 0}</div>
                <div className="text-[12px] text-muted-foreground mt-1.5">Trademarks</div>
              </div>
              <ArrowRight size={16} className="text-muted-foreground ml-6" />
            </Link>
            <Link to="/oppositions" className="bg-panel border border-border rounded-[12px] px-4.5 py-4 flex items-center justify-between hover:border-border-strong w-fit min-w-[220px]">
              <div>
                <div className="font-display text-[26px] font-bold leading-none">{data.opposed_count ?? 0}</div>
                <div className="text-[12px] text-muted-foreground mt-1.5">Opposed</div>
              </div>
              <ArrowRight size={16} className="text-muted-foreground ml-6" />
            </Link>
          </div>

          <div className="card">
            <div className="card-head">
              <div>
                <h3>Upcoming renewals</h3>
                <div className="text-[12px] text-muted-foreground mt-0.5">Next 7 days · {data.upcoming_renewals?.total_30d ?? 0} due within 30 days</div>
              </div>
            </div>
            <div className="grid grid-cols-7 gap-2">
              {renewalDays.map((day) => {
                const rows = renewalsByDate[day.key] || [];
                return (
                  <div key={day.key} className="border border-border rounded-md p-2 min-h-[110px] flex flex-col">
                    <div className="text-[10.5px] font-mono text-muted-foreground mb-1.5">{day.label}</div>
                    {rows.length === 0 ? (
                      <div className="text-[10.5px] text-muted-foreground/60 mt-1">No renewals</div>
                    ) : (
                      <div className="flex flex-col gap-1">
                        {rows.map((r: any) => (
                          <button
                            key={r.id}
                            type="button"
                            onClick={() => setOpenTmId(r.id)}
                            className="text-left bg-status-info-bg text-status-info rounded px-1.5 py-1 hover:opacity-80"
                          >
                            <div className="text-[10.5px] font-semibold truncate">{r.word_mark || r.label || r.app_no}</div>
                            <div className="text-[9.5px] font-mono opacity-80 truncate">#{r.app_no}</div>
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <h3>Recent reminders</h3>
              <Link to="/tasks" className="text-[11.5px] text-muted-foreground font-medium hover:text-primary">View All &rarr;</Link>
            </div>
            {data.recent_reminders?.length ? (
              <div className="flex flex-col gap-2">
                {data.recent_reminders.map((r: any, i: number) => (
                  <div key={i} onClick={() => setOpenTmId(r.tm_id)} className="flex justify-between items-center p-2.5 border border-border rounded-md hover:border-border-strong hover:bg-muted/50 transition-colors cursor-pointer">
                    <div>
                      <div className="text-[12.5px] font-semibold">{r.word_mark || r.app_no}</div>
                      <div className="text-[11.5px] text-muted-foreground mt-0.5">{r.subject}</div>
                    </div>
                    <div className="text-[11px] font-mono text-muted-foreground bg-muted px-2 py-1 rounded">
                      {r.despatch_date || r.corres_date?.split(' ')[0]}
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <p className="font-mono text-muted-foreground text-[12px]">No tasks found.</p>
            )}
          </div>

          <div className="card">
            <div className="card-head">
              <div>
                <h3>Monthly filing report</h3>
                <div className="text-[12px] text-muted-foreground mt-0.5">Monthly trend for the last 30 months</div>
              </div>
              <span className="font-mono text-[16px] font-semibold">Total {data.total ?? 0}</span>
            </div>
            {filingCounts.length ? (
              <>
                <div className="flex items-end gap-[3px] h-[150px] mt-1.5">
                  {filingCounts.map((row: any, i: number) => (
                    <div
                      key={i}
                      title={`${row.n} in ${row.ym}`}
                      className="flex-1 max-w-4 rounded-t-[3px] min-h-[2px]"
                      style={{ background: 'var(--color-accent)', height: `${maxN ? Math.max((row.n / maxN) * 100, 4) : 4}%` }}
                    />
                  ))}
                </div>
                <div className="flex justify-between text-[9.5px] text-muted-foreground mt-1.5">
                  {filingCounts
                    .filter((_: any, i: number) => i % Math.max(1, Math.ceil(filingCounts.length / 6)) === 0 || i === filingCounts.length - 1)
                    .map((row: any, i: number) => (
                      <span key={i}>{row.ym}</span>
                    ))}
                </div>
              </>
            ) : (
              <p className="font-mono text-muted-foreground text-[12px]">No filings tracked yet.</p>
            )}
          </div>

          <div className="grid grid-cols-3 gap-4 mt-4">
            <div className="card mb-0 flex flex-col h-[350px]">
              <div className="card-head flex-shrink-0"><h3>Status-wise</h3><span className="text-[12px] text-muted-foreground">{data.total ?? 0}</span></div>
              {data.portal_status_counts?.length ? (
                <>
                  <div className="flex justify-center py-2 flex-shrink-0"><Donut rows={data.portal_status_counts} valueKey="n" labelKey="status" /></div>
                  <div className="overflow-y-auto flex-1 pr-1 [&::-webkit-scrollbar]:hidden">
                    {data.portal_status_counts.map((row: any, i: number) => (
                      <Link
                        key={i}
                        to={`/trademarks?status=${encodeURIComponent(row.status)}`}
                        className="flex justify-between items-center py-2 border-b border-border last:border-b-0 text-[12.5px] hover:bg-muted/50 -mx-1 px-1 rounded"
                      >
                        <span className="flex items-center gap-1.5">
                          <span className="w-2.5 h-2.5 rounded-full flex-shrink-0" style={{ background: DONUT_COLORS[i % DONUT_COLORS.length] }} />
                          {row.status}
                        </span>
                        <span className="font-mono text-muted-foreground">{row.n}</span>
                      </Link>
                    ))}
                  </div>
                </>
              ) : (
                <p className="font-mono text-muted-foreground text-[12px]">No status data yet.</p>
              )}
            </div>
            <div className="card mb-0 flex flex-col h-[350px]">
              <div className="card-head flex-shrink-0"><h3>Class-wise</h3><span className="text-[12px] text-muted-foreground">{data.class_counts?.length ?? 0}</span></div>
              {data.class_counts?.length ? (
                <>
                  <div className="flex justify-center py-2 flex-shrink-0"><Donut rows={data.class_counts} valueKey="n" labelKey="class_number" /></div>
                  <div className="overflow-y-auto flex-1 pr-1 [&::-webkit-scrollbar]:hidden">
                    {data.class_counts.map((row: any, i: number) => (
                      <Link
                        key={i}
                        to={`/trademarks?class=${encodeURIComponent(row.class_number)}`}
                        className="flex justify-between items-center py-2 border-b border-border last:border-b-0 text-[12.5px] hover:bg-muted/50 -mx-1 px-1 rounded"
                      >
                        <span className="flex items-center gap-1.5">
                          <span className="w-2.5 h-2.5 rounded-full flex-shrink-0" style={{ background: DONUT_COLORS[i % DONUT_COLORS.length] }} />
                          Class {row.class_number}
                        </span>
                        <span className="font-mono text-muted-foreground">{row.n}</span>
                      </Link>
                    ))}
                  </div>
                </>
              ) : (
                <p className="font-mono text-muted-foreground text-[12px]">No class data yet.</p>
              )}
            </div>
            <div className="card mb-0 flex flex-col h-[350px]">
              <div className="card-head flex-shrink-0"><h3>State-wise</h3><span className="text-[12px] text-muted-foreground">{data.state_counts?.length ?? 0}</span></div>
              <div className="overflow-y-auto flex-1 pr-1 [&::-webkit-scrollbar]:hidden">
                {data.state_counts?.length ? (
                  data.state_counts.slice(0, 8).map((row: any, i: number) => (
                    <Link
                      key={i}
                      to={`/trademarks?state=${encodeURIComponent(row.state)}`}
                      className="flex justify-between items-center py-2 border-b border-border last:border-b-0 text-[12.5px] hover:bg-muted/50 -mx-1 px-1 rounded"
                    >
                      <span className="flex items-center gap-1.5"><span className="text-muted-foreground font-mono text-[11px] w-4">{i + 1}.</span>{row.state}</span>
                      <span className="font-mono text-muted-foreground">{row.n}</span>
                    </Link>
                  ))
                ) : (
                  <p className="font-mono text-muted-foreground text-[12px]">No state data yet.</p>
                )}
              </div>
            </div>
          </div>
        </div>

        <div className="flex flex-col gap-4">
          <div className="card bg-primary/[0.03] border-primary/15">
            <div className="card-head mb-3">
              <h3 className="text-primary">Daily Snapshot</h3>
              <span className="eyebrow text-primary/60">{todayStr}</span>
            </div>
            <div className="flex flex-col gap-2.5">
              <div className="flex justify-between items-center">
                <span className="text-[12.5px] text-primary/80 font-medium">New Status Changes</span>
                <span className="font-display text-[15px] font-bold text-primary">{statusChangesToday}</span>
              </div>
              <div className="flex justify-between items-center">
                <span className="text-[12.5px] text-primary/80 font-medium">New Documents</span>
                <span className="font-display text-[15px] font-bold text-primary">{docsToday}</span>
              </div>
              <div className="flex justify-between items-center">
                <span className="text-[12.5px] text-primary/80 font-medium">Compliance Notices</span>
                <span className="font-display text-[15px] font-bold text-primary">{tasksToday}</span>
              </div>
            </div>
            {statusChangesToday === 0 && docsToday === 0 && tasksToday === 0 && (
              <div className="mt-3 text-[11.5px] text-primary/60 flex items-center gap-1.5"><CheckCircle2 size={13} /> Portfolio is completely up to date today.</div>
            )}
          </div>

          <div className="card">
            <div className="card-head"><h3>Recent status updates</h3><Link to="/updates/statuses" className="text-[11.5px] text-muted-foreground font-medium hover:text-primary">View All &rarr;</Link></div>
            {data.status_changes?.length ? (
              (() => {
                let lastDate = '';
                return data.status_changes.slice(0, 8).map((c: any, i: number) => {
                  const d = c.changed_at ? new Date(c.changed_at).toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' }) : '—';
                  const showDate = d !== lastDate;
                  lastDate = d;
                  return (
                    <div key={i}>
                      {showDate && <div className={`eyebrow text-muted-foreground! mb-1 ${i !== 0 ? 'mt-3' : ''}`}>{d}</div>}
                      <div onClick={() => setOpenTmId(c.id)} className="flex justify-between items-center py-1 text-[12.5px] gap-2.5 hover:text-primary cursor-pointer">
                        <span className="font-medium truncate flex-1">{c.word_mark || c.app_no}</span>
                        <span className="text-muted-foreground text-[11.5px] text-right flex-shrink-0">
                          {c.old_status} &rarr; <b className="text-status-good font-medium">{c.new_status}</b>
                        </span>
                      </div>
                    </div>
                  );
                });
              })()
            ) : (
              <p className="font-mono text-muted-foreground text-[12px]">No recent updates.</p>
            )}
          </div>

          <div className="card">
            <div className="card-head"><h3>Recent document updates</h3><Link to="/updates/documents" className="text-[11.5px] text-muted-foreground font-medium hover:text-primary">View All &rarr;</Link></div>
            {data.recent_docs?.length ? (
              <div className="flex flex-col gap-3 mt-1">
                {data.recent_docs.map((d: any, i: number) => (
                  <div key={i} onClick={() => setOpenTmId(d.tm_id)} className="flex gap-2.5 group cursor-pointer">
                    <FileText size={16} className="text-muted-foreground group-hover:text-primary mt-0.5" />
                    <div className="min-w-0">
                      <div className="text-[12.5px] font-medium group-hover:text-primary truncate">{d.description || 'Document'}</div>
                      <div className="text-[11.5px] text-muted-foreground flex gap-1.5 items-center mt-0.5">
                        <span className="font-mono">{d.date_of_document ? new Date(d.date_of_document).toLocaleDateString('en-GB') : '—'}</span>
                        <span>•</span>
                        <span className="truncate">{d.word_mark || d.app_no}</span>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <p className="font-mono text-muted-foreground text-[12px]">No documents on file.</p>
            )}
          </div>
        </div>
      </div>
      {openTmId && <TrademarkOverlay tmId={openTmId} onClose={() => setOpenTmId(null)} />}
    </div>
  );
}
