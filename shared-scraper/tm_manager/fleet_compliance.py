"""Portfolio-scoped events and private receipt/deadline confirmations."""
from datetime import date
from psycopg.types.json import Jsonb
from compliance_facts import add_period, parse_date

CONFIRMED_SQL = "(p.confirmed_at IS NOT NULL AND p.confirmed_source_fingerprint=e.facts->>'source_fingerprint')"
CALCULATED_SQL = '''CASE WHEN p.receipt_date IS NOT NULL AND e.anchor='receipt_date' THEN
    CASE WHEN e.period_unit='months' THEN (p.receipt_date+make_interval(months=>e.period_value))::date
         WHEN e.period_unit='days' THEN p.receipt_date+e.period_value END END'''
# Last resort so a notice appears on the calendar: the date the registry sent it (despatch / correspondence
# date) plus the usual period (1 month to reply to an examination report, 2 months for a counter-statement).
# Always shown as an estimate.
def _meta_date(key):
    return (f"CASE WHEN dl.metadata->>'{key}' ~ '^[0-9]{{1,2}}[/-][0-9]{{1,2}}[/-][0-9]{{4}}$' "
            f"THEN to_date(replace(dl.metadata->>'{key}','-','/'),'DD/MM/YYYY') "
            f"WHEN dl.metadata->>'{key}' ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}' THEN (left(dl.metadata->>'{key}',10))::date END")
SENT_SQL = f"COALESCE(e.issue_date,{_meta_date('despatch_date')},{_meta_date('corres_date')},{_meta_date('date')})"
ESTIMATE_SQL = f"""CASE WHEN e.category IN ('examination_report_reply','reply_us_132') THEN {SENT_SQL}+30
         WHEN e.category='counter_statement' THEN ({SENT_SQL}+interval '2 months')::date END"""
DUE_SQL = f'COALESCE(CASE WHEN {CONFIRMED_SQL} THEN p.confirmed_due_date END,{CALCULATED_SQL},e.due_date,{ESTIMATE_SQL})'



def list_events(conn, user_id, start, end, *, limit=None, offset=0, app_no=None,
                side=None, category=None, include_review=True, exclude_done=False):
    params = {'uid': user_id, 'start': start, 'end': end, 'app': app_no, 'side': side, 'category': category,
              'review': include_review, 'exclude': exclude_done}
    base = f'''
        FROM fleet_control.compliance_events e
        JOIN public.my_trademarks t ON t.app_no=e.app_no
          AND (e.applies_to_side='both' OR t.kind=CASE WHEN e.applies_to_side='opposition_out' THEN 'opposition_out' ELSE 'portfolio' END)
        LEFT JOIN fleet_control.document_ledger dl ON dl.app_no=e.app_no AND dl.source_key=e.source_key
        LEFT JOIN public.fleet_event_preferences p ON p.event_id=e.id AND p.user_id=t.user_id
        LEFT JOIN public.ipindia_results r ON r.id=t.last_search_id
        WHERE e.active AND (%(uid)s::int IS NULL OR t.user_id=%(uid)s)
          AND (%(app)s::text IS NULL OR e.app_no=%(app)s)
          AND (%(side)s::text IS NULL OR t.kind=CASE WHEN %(side)s='opposition_out' THEN 'opposition_out' ELSE 'portfolio' END)
          AND (t.kind<>'opposition_out' OR EXISTS(SELECT 1 FROM public.fleet_proceeding_memberships m
               WHERE m.user_id=t.user_id AND m.app_no=e.app_no AND m.proceeding_key=e.proceeding_key))
          AND (%(category)s::text IS NULL OR e.category=%(category)s)
          AND ({DUE_SQL} BETWEEN %(start)s AND %(end)s
               OR (%(review)s AND {DUE_SQL} IS NULL))
          AND (NOT %(exclude)s OR COALESCE(p.status,'pending') NOT IN ('completed','closed'))
    '''
    total = conn.execute('SELECT count(*) AS n ' + base, params).fetchone()['n']
    query = f'''SELECT e.*,t.id AS tm_id,t.label,t.kind,r.data_json->0->>'Trade Mark' AS word_mark,
        COALESCE(p.status,'pending') AS status,p.receipt_date,p.confirmed_at,
        {DUE_SQL} AS effective_due_date,{CONFIRMED_SQL} AS confirmation_current,
        p.notes
    ''' + base + ' ORDER BY effective_due_date NULLS FIRST,e.id,t.id'
    if limit is not None:
        query += ' LIMIT %(limit)s OFFSET %(offset)s'
        params.update(limit=limit, offset=offset)
    items = []
    today = date.today()
    for row in conn.execute(query, params).fetchall():
        due = row.pop('effective_due_date')
        confirmed = row.pop('confirmation_current')
        stale = bool(row['confirmed_at']) and not confirmed
        row.update(source='event', source_id=row['id'], subject=row['facts'].get('subject'),
                   due_date=due.isoformat() if due else None, days_until=(due - today).days if due else None,
                   is_estimated=not confirmed and row['due_basis'] != 'stated',
                   needs_review=stale or (not confirmed and (row['needs_review'] or not due)),
                   due_basis='confirmation_stale' if stale else 'confirmed' if confirmed else 'receipt_period' if row['receipt_date'] and due else 'estimated' if due and row['due_basis'] == 'unknown' else row['due_basis'],
                   document_url='/api/compliance-events/' + str(row['id']) + '/document')
        for key in ('receipt_date', 'confirmed_at', 'issue_date', 'event_date', 'observed_at'):
            if row.get(key) is not None:
                row[key] = row[key].isoformat()
        items.append(row)
    return {'items': items, 'total': total, 'limit': limit, 'offset': offset}


def update_event(conn, event_id, user_id, body):
    if user_id is None:
        raise ValueError('A user identity is required for private confirmations')
    event = conn.execute('''SELECT e.* FROM fleet_control.compliance_events e
        WHERE e.id=%s AND e.active AND EXISTS(SELECT 1 FROM public.my_trademarks t
        WHERE t.app_no=e.app_no AND t.user_id=%s AND
        (e.applies_to_side='both' OR t.kind=CASE WHEN e.applies_to_side='opposition_out' THEN 'opposition_out' ELSE 'portfolio' END)
        AND (t.kind<>'opposition_out' OR EXISTS(SELECT 1 FROM public.fleet_proceeding_memberships m
             WHERE m.user_id=t.user_id AND m.app_no=e.app_no AND m.proceeding_key=e.proceeding_key)))''',
                         (event_id, user_id)).fetchone()
    if not event:
        return False
    conn.execute('INSERT INTO public.fleet_event_preferences(event_id,user_id) VALUES(%s,%s) ON CONFLICT DO NOTHING', (event_id, user_id))
    old = conn.execute('SELECT * FROM public.fleet_event_preferences WHERE event_id=%s AND user_id=%s FOR UPDATE', (event_id, user_id)).fetchone()
    status = body.get('status', old['status'])
    if status not in ('pending', 'in_progress', 'completed', 'closed'):
        raise ValueError('Invalid task status')
    receipt = parse_date(body['receipt_date']) if body.get('receipt_date') else None if 'receipt_date' in body else old['receipt_date']
    if body.get('receipt_date') and not receipt:
        raise ValueError('Invalid receipt date')
    if receipt and receipt > date.today():
        raise ValueError('Receipt date cannot be in the future')
    calculated = None
    if receipt and event['anchor'] == 'receipt_date' and event['period_value'] and event['period_unit']:
        calculated = add_period(receipt, event['period_value'], event['period_unit'])
    confirmed = old['confirmed_due_date']
    confirmed_at = old['confirmed_at']
    confirmed_source = old['confirmed_source_fingerprint']
    # Changing the anchor invalidates the previous confirmation.
    if receipt != old['receipt_date']:
        confirmed, confirmed_at, confirmed_source = None, None, None
    if 'confirmed_due_date' in body:
        confirmed = parse_date(body['confirmed_due_date']) if body['confirmed_due_date'] else None
        if body['confirmed_due_date'] and not confirmed:
            raise ValueError('Invalid confirmed due date')
        from datetime import datetime, timezone
        confirmed_at = datetime.now(timezone.utc) if confirmed else None
        confirmed_source = event['facts'].get('source_fingerprint') if confirmed else None
    notes = body.get('notes', old['notes'])
    if not isinstance(notes, str) or len(notes) > 4000:
        raise ValueError('Notes must be at most 4000 characters')
    values = {'status': status, 'receipt_date': receipt, 'calculated_due_date': calculated,
              'confirmed_due_date': confirmed, 'confirmed_at': confirmed_at, 'notes': notes, 'confirmed_source_fingerprint': confirmed_source}
    changes = {k: {'from': str(old[k]) if old[k] is not None else None, 'to': str(v) if v is not None else None}
               for k, v in values.items() if old[k] != v}
    conn.execute('''UPDATE public.fleet_event_preferences SET status=%s,receipt_date=%s,calculated_due_date=%s,
        confirmed_due_date=%s,confirmed_at=%s,notes=%s,confirmed_source_fingerprint=%s,updated_at=now() WHERE event_id=%s AND user_id=%s''',
                 (*values.values(), event_id, user_id))
    if changes:
        conn.execute('INSERT INTO public.fleet_event_preference_audit(event_id,user_id,changes) VALUES(%s,%s,%s)',
                     (event_id, user_id, Jsonb(changes)))
    conn.commit()
    return True
