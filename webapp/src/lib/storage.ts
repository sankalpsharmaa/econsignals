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

export function setFeedback(
  store: FeedbackStore,
  id: number,
  update: Partial<PaperFeedback>
): FeedbackStore {
  const current = getFeedback(store, id);
  const next = { ...current, ...update };
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
