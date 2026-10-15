import { useLocation, useNavigate, useParams } from 'react-router-dom';
import DetailPane from '../components/DetailPane';

export default function TrademarkDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const tmId = Number(id);
  const from = (location.state as { from?: string; fromLabel?: string } | null)?.from || '/trademarks';
  const fromLabel = (location.state as { from?: string; fromLabel?: string } | null)?.fromLabel || 'Portfolio';

  return (
    <div className="flex-1 min-h-0 flex flex-col overflow-hidden">
      <DetailPane
        tmId={tmId}
        onClose={() => navigate(from)}
        variant="page"
        breadcrumbLabel={fromLabel}
        breadcrumbTo={from}
      />
    </div>
  );
}
