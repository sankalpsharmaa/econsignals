import type { FeedbackStore, PaperFeedback } from '../types';

const FEEDBACK_KEY = 'econsignals:feedback';
const THEME_KEY = 'econsignals:theme';

export function loadFeedback(): FeedbackStore {
  try {
    const raw = localStorage.getItem(FEEDBACK_KEY);
    if (!raw) return {};
    return JSON.parse(raw) as FeedbackStore;
  } catch {
    return {};
  }
}

export function saveFeedback(store: FeedbackStore): void {
  localStorage.setItem(FEEDBACK_KEY, JSON.stringify(store));
}

export function getFeedback(store: FeedbackStore, id: number): PaperFeedback {
  return store[String(id)] ?? { starred: false, vote: null, hidden: false };
}

// Second-precision UTC stamp matching Python's %Y-%m-%dT%H:%M:%SZ so importing
// into data/feedback.jsonl yields lines byte-compatible with /api/feedback.
function nowStamp(): string {
  return new Date().toISOString().slice(0, 19) + 'Z';
}

export function setFeedback(
  store: FeedbackStore,
  id: number,
  update: Partial<PaperFeedback>
): FeedbackStore {
  const current = getFeedback(store, id);
  const next = { ...current, ...update };
  // Stamp the vote time only when this update sets an up/down vote, so the
  // exported store carries a stable timestamp per vote (not per export).
  // A stable stamp is what makes the Python import idempotent.
  if ('vote' in update && (update.vote === 'up' || update.vote === 'down')) {
    next.votedAt = nowStamp();
  }
  const updated = { ...store, [String(id)]: next };
  saveFeedback(updated);
  return updated;
}

export function exportFeedback(store: FeedbackStore): void {
  const blob = new Blob([JSON.stringify(store, null, 2)], {
    type: 'application/json',
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `econsignals-feedback-${new Date().toISOString().slice(0, 10)}.json`;
  a.click();
  URL.revokeObjectURL(url);
}

export type Theme = 'light' | 'dark' | 'system';

export function loadTheme(): Theme {
  const stored = localStorage.getItem(THEME_KEY);
  if (stored === 'light' || stored === 'dark') return stored;
  return 'system';
}

export function saveTheme(theme: Theme): void {
  localStorage.setItem(THEME_KEY, theme);
}
