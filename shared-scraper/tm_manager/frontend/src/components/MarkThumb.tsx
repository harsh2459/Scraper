import { useState } from 'react';
import { API_BASE } from '../lib/api';

// Every thumbnail sits in the SAME fixed-size box regardless of the actual
// logo's shape (some are wide wordmark crops ~4.5:1, some near-square, some
// a plain single-letter fallback) — object-contain scales each image DOWN
// to fit inside that fixed box without cropping or distorting it, rather
// than letting the box grow/shrink to match the image's natural size.
// Without a fixed box, each row's height/icon width changed the moment its
// own logo finished loading (async, one at a time), so a whole list
// visibly jumped around while it loaded — this keeps every row's layout
// stable from the first render, before any image has loaded at all.
export default function MarkThumb({ id, hasLogo, name, size = 44 }: { id: number; hasLogo: boolean; name: string; size?: number }) {
  const [error, setError] = useState(false);
  const showFallback = !hasLogo || error;
  return (
    <div
      className="rounded-md bg-muted flex items-center justify-center flex-shrink-0 overflow-hidden"
      style={{ width: size, height: size }}
    >
      {showFallback ? (
        <div className="w-full h-full rounded-md bg-primary text-primary-foreground flex items-center justify-center text-[14px] font-display font-semibold">
          {name && name !== '—' ? name.substring(0, 1).toUpperCase() : '?'}
        </div>
      ) : (
        <img
          src={`${API_BASE}/trademark/${id}/logo`}
          alt=""
          loading="lazy"
          className="max-w-full max-h-full w-auto h-auto object-contain"
          onError={() => setError(true)}
        />
      )}
    </div>
  );
}
