// Portal status → functional status class. TM Tracker uses only meaningful
// status colour: s-good (in force), s-bad (adverse / lapsed), s-info (neutral
// mid-pipeline). s-warn (amber) is reserved for renewal-deadline computation
// (see Tasks page) and is never assigned from raw portal status text.
const STATUS_RULES: [string, string][] = [
  ['objected', 's-bad'],
  ['opposed', 's-bad'],
  ['abandoned', 's-bad'],
  ['refused', 's-bad'],
  ['withdrawn', 's-bad'],
  ['formalities chk fail', 's-bad'],
  ['registered', 's-good'],
  ['accepted', 's-good'],
  ['renewed', 's-good'],
  ['formalities chk pass', 's-good'],
  ['marked for exam', 's-info'],
  ['exam', 's-info'],
  ['send to vienna', 's-info'],
  ['advertised', 's-info'],
];

export function statusClass(status: string | null | undefined): string {
  const s = (status || '').toLowerCase();
  for (const [substr, cls] of STATUS_RULES) {
    if (s.includes(substr)) return cls;
  }
  return 's-info';
}
