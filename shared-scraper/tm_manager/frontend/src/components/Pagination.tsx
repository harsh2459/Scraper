interface PaginationProps {
  page: number;
  totalPages: number;
  totalRows: number;
  pageRows: number;
  rowLabel?: string;
  onPrev: () => void;
  onNext: () => void;
}

export default function Pagination({
  page,
  totalPages,
  totalRows,
  pageRows,
  rowLabel = 'rows',
  onPrev,
  onNext,
}: PaginationProps) {
  if (totalPages <= 1 && totalRows <= pageRows) return null;

  const start = (page - 1) * pageRows + 1;
  const end = Math.min(page * pageRows, totalRows);

  return (
    <div className="flex justify-between items-center px-5 py-2.5 border-t border-border text-[12.5px] text-muted-foreground bg-panel flex-wrap gap-2 flex-shrink-0">
      <span>
        Showing {start}–{end} of {totalRows} {rowLabel}
      </span>
      <div className="flex items-center gap-3">
        <span>Page {page} of {totalPages}</span>
        <button
          className="w-6 h-6 border border-border-strong rounded-md flex items-center justify-center disabled:opacity-40 hover:bg-muted transition-colors"
          disabled={page <= 1}
          onClick={onPrev}
        >
          ‹
        </button>
        <button
          className="w-6 h-6 border border-border-strong rounded-md flex items-center justify-center disabled:opacity-40 hover:bg-muted transition-colors"
          disabled={page >= totalPages}
          onClick={onNext}
        >
          ›
        </button>
      </div>
    </div>
  );
}
