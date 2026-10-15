export default function Placeholder({ title, description }: { title: string; description?: string }) {
  return (
    <div className="flex-1 min-h-0 overflow-y-auto px-7.5 pt-6 pb-15">
      <div className="flex items-center justify-between gap-4 mb-5 flex-wrap">
        <h1 className="text-[19px] font-semibold m-0">{title}</h1>
      </div>
      <div className="max-w-[480px] py-10">
        <p className="text-muted-foreground text-[13.5px] leading-relaxed m-0">
          {description || 'This section is coming soon.'}
        </p>
      </div>
    </div>
  );
}
