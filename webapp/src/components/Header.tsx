import type { FeedStats } from '../types';
import type { Theme } from '../lib/storage';
import { formatDate } from '../lib/feed';

interface HeaderProps {
  stats: FeedStats | null;
  generatedAt: string | null;
  theme: Theme;
  onThemeToggle: () => void;
}

export function Header({ stats, generatedAt, theme, onThemeToggle }: HeaderProps) {
  const themeLabel =
    theme === 'system' ? 'System' : theme === 'dark' ? 'Dark' : 'Light';
  const themeIcon = theme === 'dark' ? '☾' : theme === 'light' ? '☀' : '◑';

  return (
    <header className="header" role="banner">
      <div className="header-inner">
        <div className="header-brand">
          <div className="wordmark">
            Econ<span>Signals</span>
          </div>
          <div className="tagline">
            Research intelligence — development · urban · India
          </div>
        </div>

        <div className="header-meta">
          {stats && (
            <div className="header-stats" aria-label="Feed statistics">
              <div className="stat-item">
                <span className="stat-value">{stats.papers}</span>
                <span className="stat-label">Papers</span>
              </div>
              <div className="stat-item">
                <span className="stat-value">{stats.deadlines}</span>
                <span className="stat-label">Deadlines</span>
              </div>
              <div className="stat-item">
                <span className="stat-value">{stats.social}</span>
                <span className="stat-label">Social</span>
              </div>
            </div>
          )}

          <div className="header-actions">
            {generatedAt && (
              <span className="updated-at" aria-label="Last updated">
                {formatDate(generatedAt)}
              </span>
            )}
            <button
              className="theme-toggle"
              onClick={onThemeToggle}
              aria-label={`Switch theme, currently ${themeLabel}`}
              title={`Theme: ${themeLabel}`}
            >
              <span aria-hidden="true">{themeIcon}</span>
              {themeLabel}
            </button>
          </div>
        </div>
      </div>
    </header>
  );
}
