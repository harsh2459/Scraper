// The TM Tracker signature device — one ring, used as the app mark, the
// loading spinner, and the portfolio status indicator. Geometry is fixed
// from the brand guide (viewBox 0 0 512 512, r=150, stroke-width 30).

type RingState =
  | 'loading'
  | 'in-force'
  | 'due'
  | 'grace'
  | 'restoration'
  | 'brand';

const TRACK = '#E4E8F1';
const AMBER = '#F6A21E';
const EMERALD = '#10B981';
const RED = '#E5484D';

// Near-complete countdown arc (gap at top-left) and the confirmation check.
const FULL_ARC = 'M 256 106 A 150 150 0 1 1 150 150';
const HALF_ARC = 'M 256 106 A 150 150 0 0 1 406 256';
const SHORT_ARC = 'M 256 106 A 150 150 0 0 1 322 128';
const CHECK = 'M198 260 L240 302 L322 206';
const CROSS = 'M188 188 L324 324 M324 188 L188 324';

export default function Ring({
  state,
  size = 28,
  className = '',
}: {
  state: RingState;
  size?: number;
  className?: string;
}) {
  const common = { width: size, height: size, viewBox: '0 0 512 512', className, 'aria-hidden': true } as const;

  if (state === 'brand') {
    return (
      <svg {...common}>
        <rect width="512" height="512" rx="116" fill="#0E1834" stroke="rgba(255,255,255,.10)" />
        <circle cx="256" cy="256" r="150" fill="none" stroke="#fff" strokeOpacity=".14" strokeWidth="30" />
        <path d={FULL_ARC} fill="none" stroke={AMBER} strokeWidth="30" strokeLinecap="round" />
        <circle cx="150" cy="150" r="30" fill={AMBER} />
        <circle cx="150" cy="150" r="11" fill="#0E1834" />
        <path d={CHECK} fill="none" stroke="#fff" strokeWidth="34" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }

  if (state === 'loading') {
    return (
      <svg {...common}>
        <circle cx="256" cy="256" r="150" fill="none" stroke={TRACK} strokeWidth="30" />
        <path d={FULL_ARC} fill="none" stroke={AMBER} strokeWidth="30" strokeLinecap="round">
          <animateTransform
            attributeName="transform"
            type="rotate"
            from="0 256 256"
            to="360 256 256"
            dur="1.1s"
            repeatCount="indefinite"
          />
        </path>
      </svg>
    );
  }

  if (state === 'in-force') {
    return (
      <svg {...common}>
        <circle cx="256" cy="256" r="150" fill="none" stroke={TRACK} strokeWidth="30" />
        <path d={FULL_ARC} fill="none" stroke={EMERALD} strokeWidth="30" strokeLinecap="round" />
        <path d={CHECK} fill="none" stroke={EMERALD} strokeWidth="34" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }

  if (state === 'due') {
    return (
      <svg {...common}>
        <circle cx="256" cy="256" r="150" fill="none" stroke={TRACK} strokeWidth="30" />
        <path d={FULL_ARC} fill="none" stroke={AMBER} strokeWidth="30" strokeLinecap="round" />
        <circle cx="150" cy="150" r="26" fill={AMBER} />
        <text x="256" y="298" textAnchor="middle" fontFamily="Space Grotesk, sans-serif" fontSize="170" fontWeight="700" fill={AMBER}>!</text>
      </svg>
    );
  }

  if (state === 'grace') {
    return (
      <svg {...common}>
        <circle cx="256" cy="256" r="150" fill="none" stroke={TRACK} strokeWidth="30" />
        <path d={HALF_ARC} fill="none" stroke={RED} strokeWidth="30" strokeLinecap="round" />
        <circle cx="406" cy="256" r="26" fill={RED} />
        <text x="256" y="308" textAnchor="middle" fontFamily="Space Grotesk, sans-serif" fontSize="170" fontWeight="700" fill={RED}>!</text>
      </svg>
    );
  }

  // restoration
  return (
    <svg {...common}>
      <circle cx="256" cy="256" r="150" fill="none" stroke={TRACK} strokeWidth="30" />
      <path d={SHORT_ARC} fill="none" stroke={RED} strokeWidth="30" strokeLinecap="round" />
      <path d={CROSS} stroke={RED} strokeWidth="34" strokeLinecap="round" />
    </svg>
  );
}

// Map a statusClass() result (s-good / s-warn / s-bad / s-info) to a ring state,
// for row/detail status indicators.
export function ringStateForClass(cls: string): RingState {
  if (cls === 's-good') return 'in-force';
  if (cls === 's-warn') return 'due';
  if (cls === 's-bad') return 'grace';
  return 'in-force';
}
