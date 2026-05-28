import type { Tab } from '../types';

interface TabBarProps {
  active: Tab;
  onChange: (tab: Tab) => void;
  counts: { papers: number; deadlines: number; social: number; sensors: number };
}

const TABS: { id: Tab; label: string; countKey: keyof TabBarProps['counts'] }[] = [
  { id: 'papers', label: 'Papers', countKey: 'papers' },
  { id: 'deadlines', label: 'Deadlines', countKey: 'deadlines' },
  { id: 'social', label: 'Social', countKey: 'social' },
  { id: 'status', label: 'Status', countKey: 'sensors' },
];

export function TabBar({ active, onChange, counts }: TabBarProps) {
  function handleKey(e: React.KeyboardEvent<HTMLButtonElement>, tab: Tab) {
    const tabs = TABS.map((t) => t.id);
    const idx = tabs.indexOf(tab);
    if (e.key === 'ArrowRight') {
      onChange(tabs[(idx + 1) % tabs.length]);
    } else if (e.key === 'ArrowLeft') {
      onChange(tabs[(idx - 1 + tabs.length) % tabs.length]);
    }
  }

  return (
    <nav className="tabs-bar" aria-label="Main navigation">
      <div className="tabs-inner" role="tablist">
        {TABS.map(({ id, label, countKey }) => (
          <button
            key={id}
            role="tab"
            aria-selected={active === id}
            aria-controls={`panel-${id}`}
            id={`tab-${id}`}
            className="tab-btn"
            onClick={() => onChange(id)}
            onKeyDown={(e) => handleKey(e, id)}
            tabIndex={active === id ? 0 : -1}
          >
            {label}
            {counts[countKey] > 0 && (
              <span className="tab-badge" aria-label={`${counts[countKey]} items`}>
                {counts[countKey]}
              </span>
            )}
          </button>
        ))}
      </div>
    </nav>
  );
}
