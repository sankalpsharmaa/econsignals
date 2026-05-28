import { useState, useEffect, useCallback } from 'react';
import './App.css';
import type { Feed, Tab, FeedbackStore } from './types';
import { fetchFeed } from './lib/feed';
import {
  loadFeedback,
  loadTheme,
  saveTheme,
  type Theme,
} from './lib/storage';
import { Header } from './components/Header';
import { TabBar } from './components/TabBar';
import { PapersView } from './components/PapersView';
import { DeadlinesView } from './components/DeadlinesView';
import { SocialView } from './components/SocialView';
import { StatusView } from './components/StatusView';

function resolveTheme(theme: Theme): 'light' | 'dark' {
  if (theme === 'system') {
    return window.matchMedia('(prefers-color-scheme: dark)').matches
      ? 'dark'
      : 'light';
  }
  return theme;
}

export default function App() {
  const [feed, setFeed] = useState<Feed | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<Tab>('papers');
  const [feedback, setFeedback] = useState<FeedbackStore>(() => loadFeedback());
  const [theme, setTheme] = useState<Theme>(() => loadTheme());

  // Apply theme to document
  useEffect(() => {
    const resolved = resolveTheme(theme);
    document.documentElement.setAttribute('data-theme', resolved);
  }, [theme]);

  // System theme change listener
  useEffect(() => {
    if (theme !== 'system') return;
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const handler = () => {
      document.documentElement.setAttribute(
        'data-theme',
        mq.matches ? 'dark' : 'light'
      );
    };
    mq.addEventListener('change', handler);
    return () => mq.removeEventListener('change', handler);
  }, [theme]);

  useEffect(() => {
    fetchFeed()
      .then((data) => {
        setFeed(data);
        setLoading(false);
      })
      .catch((err: unknown) => {
        setError(err instanceof Error ? err.message : 'Failed to load feed');
        setLoading(false);
      });
  }, []);

  const handleThemeToggle = useCallback(() => {
    setTheme((prev) => {
      const next: Theme =
        prev === 'system' ? 'light' : prev === 'light' ? 'dark' : 'system';
      saveTheme(next);
      return next;
    });
  }, []);

  const handleFeedbackChange = useCallback((store: FeedbackStore) => {
    setFeedback(store);
  }, []);

  const counts = {
    papers: feed?.papers.length ?? 0,
    deadlines: feed?.deadlines.length ?? 0,
    social: feed?.social.length ?? 0,
    sensors: feed?.sensors.length ?? 0,
  };

  return (
    <div className="app">
      <Header
        stats={feed?.stats ?? null}
        generatedAt={feed?.generated_at ?? null}
        theme={theme}
        onThemeToggle={handleThemeToggle}
      />

      <TabBar
        active={activeTab}
        onChange={setActiveTab}
        counts={counts}
      />

      <main className="main" id="main-content">
        <div className="container">
          {loading && (
            <div className="state-message" role="status" aria-live="polite">
              <div className="loading-spinner" aria-hidden="true" />
              <h3>Loading feed…</h3>
            </div>
          )}

          {error && !loading && (
            <div className="state-message" role="alert">
              <h3>Failed to load feed</h3>
              <p>{error}</p>
            </div>
          )}

          {feed && !loading && (
            <>
              <div
                role="tabpanel"
                id="panel-papers"
                aria-labelledby="tab-papers"
                hidden={activeTab !== 'papers'}
              >
                {activeTab === 'papers' && (
                  <PapersView
                    papers={feed.papers}
                    feedback={feedback}
                    onFeedbackChange={handleFeedbackChange}
                  />
                )}
              </div>

              <div
                role="tabpanel"
                id="panel-deadlines"
                aria-labelledby="tab-deadlines"
                hidden={activeTab !== 'deadlines'}
              >
                {activeTab === 'deadlines' && (
                  <DeadlinesView deadlines={feed.deadlines} />
                )}
              </div>

              <div
                role="tabpanel"
                id="panel-social"
                aria-labelledby="tab-social"
                hidden={activeTab !== 'social'}
              >
                {activeTab === 'social' && (
                  <SocialView items={feed.social} />
                )}
              </div>

              <div
                role="tabpanel"
                id="panel-status"
                aria-labelledby="tab-status"
                hidden={activeTab !== 'status'}
              >
                {activeTab === 'status' && (
                  <StatusView
                    sensors={feed.sensors}
                    generatedAt={feed.generated_at}
                  />
                )}
              </div>
            </>
          )}
        </div>
      </main>
    </div>
  );
}
