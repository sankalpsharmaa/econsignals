import type { Feed } from '../types';

export async function fetchFeed(): Promise<Feed> {
  const apiBase = import.meta.env.VITE_API_BASE as string | undefined;
  const url = apiBase
    ? `${apiBase}/api/feed`
    : `${import.meta.env.BASE_URL}feed.json`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Failed to fetch feed: ${res.status}`);
  return res.json() as Promise<Feed>;
}

export function formatDate(iso: string | null): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  });
}

export function stripHandle(handle: string): string {
  // strip leading @@ or @ so we always display a single @
  return handle.replace(/^@@?/, '@');
}
