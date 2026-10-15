export function Field({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <div className="text-[12px] text-muted-foreground mb-0.5">{label}</div>
      <div className={`text-[13.5px] ${mono ? 'font-mono' : ''}`}>{value}</div>
    </div>
  );
}
