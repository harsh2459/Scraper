import DetailPane from './DetailPane';

export default function TrademarkOverlay({ tmId, onClose }: { tmId: number; onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/30" onClick={onClose}>
      <div
        className="w-[900px] max-w-[90vw] h-full bg-panel shadow-2xl flex flex-col min-h-0"
        onClick={(e) => e.stopPropagation()}
      >
        <DetailPane tmId={tmId} onClose={onClose} />
      </div>
    </div>
  );
}
