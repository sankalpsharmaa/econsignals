import { useState, useMemo } from 'react';
import type { Paper, FeedbackStore, SortOrder } from '../types';
import { getFeedback, setFeedback, exportFeedback } from '../lib/storage';
import { PaperCard } from './PaperCard';

interface PapersViewProps {
  papers: Paper[];
  feedback: FeedbackStore;
  onFeedbackChange: (store: FeedbackStore) => void;
}

export function PapersView({ papers, feedback, onFeedbackChange }: PapersViewProps) {
  const [search, setSearch] = useState('');
  const [selectedSources, setSelectedSources] = useState<Set<string>>(new Set());
  const [selectedTopics, setSelectedTopics] = useState<Set<string>>(new Set());
  const [indiaOnly, setIndiaOnly] = useState(false);
  const [minScore, setMinScore] = useState(0);
  const [sort, setSort] = useState<SortOrder>('relevance');
  const [starredOnly, setStarredOnly] = useState(false);
  const [showHidden, setShowHidden] = useState(false);

  const allSources = useMemo(() => {
    const s = new Set<string>();
    papers.forEach((p) => {
      if (p.source) s.add(p.source);
    });
    return Array.from(s).sort();
  }, [papers]);

  const allTopics = useMemo(() => {
    const t = new Set<string>();
    papers.forEach((p) => p.topics.forEach((tp) => t.add(tp)));
    return Array.from(t).sort();
  }, [papers]);

  function toggleSource(source: string) {
    setSelectedSources((prev) => {
      const next = new Set(prev);
      if (next.has(source)) next.delete(source);
      else next.add(source);
      return next;
    });
  }

  function toggleTopic(topic: string) {
    setSelectedTopics((prev) => {
      const next = new Set(prev);
      if (next.has(topic)) next.delete(topic);
      else next.add(topic);
      return next;
    });
  }

  const hiddenCount = useMemo(
    () => papers.filter((p) => getFeedback(feedback, p.id).hidden).length,
    [papers, feedback]
  );

  const filtered = useMemo(() => {
    const q = search.toLowerCase();
    let result = papers.filter((p) => {
      const fb = getFeedback(feedback, p.id);
      if (!showHidden && fb.hidden) return false;
      if (starredOnly && !fb.starred) return false;
      if (indiaOnly && !p.india) return false;
      if (selectedSources.size > 0 && (!p.source || !selectedSources.has(p.source))) return false;
      if (p.score < minScore) return false;
      if (selectedTopics.size > 0) {
        const hasAll = Array.from(selectedTopics).every((t) =>
          p.topics.includes(t)
        );
        if (!hasAll) return false;
      }
      if (q) {
        const haystack = [p.title, ...p.authors, p.abstract].join(' ').toLowerCase();
        if (!haystack.includes(q)) return false;
      }
      return true;
    });

    if (sort === 'relevance') {
      result = [...result].sort((a, b) => b.score - a.score);
    } else if (sort === 'newest') {
      result = [...result].sort((a, b) => {
        const da = a.published_at ?? '';
        const db = b.published_at ?? '';
        return db.localeCompare(da);
      });
    } else if (sort === 'title') {
      result = [...result].sort((a, b) => a.title.localeCompare(b.title));
    }

    return result;
  }, [papers, feedback, search, selectedSources, selectedTopics, indiaOnly, minScore, sort, starredOnly, showHidden]);

  function handleFeedback(id: number, update: Partial<(typeof feedback)[string]>) {
    const next = setFeedback(feedback, id, update);
    onFeedbackChange(next);
  }

  return (
    <div>
      <div className="papers-controls" role="search" aria-label="Filter papers">
        <div className="controls-row">
          <input
            type="search"
            className="search-box"
            placeholder="Search title, author, abstract…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            aria-label="Search papers"
          />
          <select
            className="control-select"
            value={sort}
            onChange={(e) => setSort(e.target.value as SortOrder)}
            aria-label="Sort order"
          >
            <option value="relevance">Relevance</option>
            <option value="newest">Newest</option>
            <option value="title">Title</option>
          </select>
        </div>

        {allSources.length > 0 && (
          <div className="topic-filters" role="group" aria-label="Filter by source">
            {allSources.map((s) => (
              <button
                key={s}
                className={`topic-chip${selectedSources.has(s) ? ' selected' : ''}`}
                onClick={() => toggleSource(s)}
                aria-pressed={selectedSources.has(s)}
              >
                {s}
              </button>
            ))}
          </div>
        )}

        <div className="controls-row">
          <button
            className={`control-btn${indiaOnly ? ' active' : ''}`}
            onClick={() => setIndiaOnly((v) => !v)}
            aria-pressed={indiaOnly}
          >
            India only
          </button>
          <button
            className={`control-btn${starredOnly ? ' active' : ''}`}
            onClick={() => setStarredOnly((v) => !v)}
            aria-pressed={starredOnly}
          >
            ★ Saved
          </button>
          <button
            className="control-btn"
            onClick={() => exportFeedback(feedback)}
            title="Download feedback as JSON for retraining"
            aria-label="Export feedback JSON"
          >
            ↓ Export feedback
          </button>

          <div className="score-row">
            <label className="score-label" htmlFor="min-score">
              Min score: {Math.round(minScore * 100)}%
            </label>
            <input
              id="min-score"
              type="range"
              className="score-slider"
              min={0}
              max={1}
              step={0.05}
              value={minScore}
              onChange={(e) => setMinScore(Number(e.target.value))}
              aria-label="Minimum relevance score"
            />
          </div>
        </div>

        {allTopics.length > 0 && (
          <div className="topic-filters" role="group" aria-label="Filter by topic">
            {allTopics.map((t) => (
              <button
                key={t}
                className={`topic-chip${selectedTopics.has(t) ? ' selected' : ''}`}
                onClick={() => toggleTopic(t)}
                aria-pressed={selectedTopics.has(t)}
              >
                {t}
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="results-meta">
        <span>
          {filtered.length} paper{filtered.length !== 1 ? 's' : ''}
        </span>
        {hiddenCount > 0 && !showHidden && (
          <button
            className="hidden-toggle"
            onClick={() => setShowHidden(true)}
          >
            {hiddenCount} hidden — show
          </button>
        )}
        {showHidden && (
          <button
            className="hidden-toggle"
            onClick={() => setShowHidden(false)}
          >
            Hide hidden papers
          </button>
        )}
      </div>

      <div role="feed" aria-label="Papers feed">
        {filtered.length === 0 ? (
          <div className="state-message">
            <h3>No papers match</h3>
            <p>Try adjusting your search or filters.</p>
          </div>
        ) : (
          filtered.map((p) => (
            <PaperCard
              key={p.id}
              paper={p}
              feedback={getFeedback(feedback, p.id)}
              onFeedback={(upd) => handleFeedback(p.id, upd)}
            />
          ))
        )}
      </div>
    </div>
  );
}
