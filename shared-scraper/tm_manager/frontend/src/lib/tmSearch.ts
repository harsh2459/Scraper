export function wmOptionLabel(o: string): string {
  return o === 'start_with' ? 'Start With' : o === 'match_with' ? 'Match With' : 'Contains';
}
