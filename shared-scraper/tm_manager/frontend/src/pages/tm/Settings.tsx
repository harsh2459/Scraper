import { useEffect, useRef, useState } from 'react';
import Ring from '../../components/tm/Ring';
import { ImageUp, X } from 'lucide-react';
import { tmApi } from '../../lib/tmApi';

const inputClass = 'border border-border-strong rounded-md px-2.5 py-1.5 text-[13px] outline-none focus:border-primary w-full';

const FIELDS: { key: string; label: string }[] = [
  { key: 'company_name', label: 'Company Name' },
  { key: 'company_address_1', label: 'Address Line 1' },
  { key: 'company_address_2', label: 'Address Line 2' },
  { key: 'company_city', label: 'City / Pincode' },
  { key: 'company_email', label: 'Email' },
];

function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

// Set once here, shown on every invoice from here on — there is no
// per-invoice copy of these fields, so this page is the only place they're
// ever edited.
export default function Settings() {
  const [values, setValues] = useState<Record<string, string> | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const logoInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => { tmApi.getSettings().then(setValues).catch((err) => console.error(err)); }, []);

  if (!values) return <div className="loading"><Ring state="loading" size={28} /><span>Loading settings…</span></div>;

  const save = async (overrides?: Record<string, string>) => {
    setSaving(true);
    setSaved(false);
    try {
      const updated = await tmApi.updateSettings({ ...values, ...overrides });
      setValues(updated);
      setSaved(true);
    } finally {
      setSaving(false);
    }
  };

  const onLogoChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const dataUrl = await readFileAsDataUrl(file);
    setValues({ ...values, company_logo: dataUrl });
    await save({ company_logo: dataUrl });
    e.target.value = '';
  };

  const removeLogo = async () => {
    setValues({ ...values, company_logo: '' });
    await save({ company_logo: '' });
  };

  return (
    <div className="flex-1 min-h-0 overflow-y-auto px-7.5 pt-6 pb-15">
      <h1 className="text-[19px] font-semibold m-0 mb-1">Settings — Company Branding</h1>
      <p className="text-muted-foreground text-[13px] mb-5">Set this once — it appears on every invoice automatically.</p>

      <div className="card max-w-[560px]">
        <label className="flex flex-col gap-1.5 mb-3.5">
          <span className="eyebrow text-muted-foreground!">Logo</span>
          <div className="flex items-center gap-3">
            {values.company_logo ? (
              <div className="relative group">
                <img src={values.company_logo} alt="" className="h-14 max-w-[200px] object-contain border border-border rounded-md p-1.5" />
                <button
                  type="button"
                  onClick={removeLogo}
                  className="absolute -top-2 -right-2 w-5 h-5 rounded-full bg-status-bad text-white flex items-center justify-center opacity-0 group-hover:opacity-100"
                  title="Remove logo"
                >
                  <X size={11} />
                </button>
              </div>
            ) : (
              <div className="h-14 w-32 border border-dashed border-border-strong rounded-md flex items-center justify-center text-[11px] text-muted-foreground">No logo</div>
            )}
            <button type="button" className="btn" onClick={() => logoInputRef.current?.click()} disabled={saving}>
              <ImageUp size={13} /> {values.company_logo ? 'Change Logo' : 'Upload Logo'}
            </button>
            <input ref={logoInputRef} type="file" accept="image/*" className="hidden" onChange={onLogoChange} />
          </div>
        </label>

        <div className="flex flex-col gap-3.5">
          {FIELDS.map((f) => (
            <label key={f.key} className="flex flex-col gap-1.5">
              <span className="eyebrow text-muted-foreground!">{f.label}</span>
              <input
                className={inputClass}
                value={values[f.key] || ''}
                onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}
              />
            </label>
          ))}
        </div>
        <div className="flex items-center gap-3 mt-4">
          <button type="button" className="btn btn-accent" disabled={saving} onClick={() => save()}>{saving ? 'Saving…' : 'Save Settings'}</button>
          {saved && <span className="text-[12.5px] text-status-good">Saved — applied to all invoices.</span>}
        </div>
      </div>
    </div>
  );
}
