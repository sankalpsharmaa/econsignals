import { useState } from 'react';
import type { Paper, PaperFeedback } from '../types';
import { formatDate } from '../lib/feed';

interface PaperCardProps {
  paper: Paper;
  feedback: PaperFeedback;
  onFeedback: (update: Partial<PaperFeedback>) => void;
}

function scoreClass(score: number): string {
  if (score >= 0.75) return 'high';
  if (score >= 0.4) return 'mid';
  return 'low';
}

export function PaperCard({ paper, feedback, onFeedback }: PaperCardProps) {
  const [showAbstract, setShowAbstract] = useState(false);

  const href = paper.url ?? (paper.doi ? `https://doi.org/${paper.doi}` : null);
  const displayAuthors =
    paper.authors.length <= 3
      ? paper.authors.join(', ')
      : `${paper.authors.slice(0, 3).join(', ')} +${paper.authors.length - 3}`;

  const titleId = `paper-title-${paper.id}`;

  return (
    <article
      className="paper-card"
      aria-labelledby={titleId}
    >
      <div className="paper-header">
        <div className="paper-title" id={titleId}>
          {href ? (
            <a href={href} target="_blank" rel="noopener noreferrer">
              {paper.title}
            </a>
          ) : (
            paper.title
          )}
        </div>

        <div className="paper-actions" role="group" aria-label="Paper actions">
          <button
            className={`action-btn${feedback.starred ? ' starred' : ''}`}
            onClick={() => onFeedback({ starred: !feedback.starred })}
            aria-label={feedback.starred ? 'Unstar paper' : 'Star paper'}
            aria-pressed={feedback.starred}
            title={feedback.starred ? 'Unstar' : 'Star'}
          >
            {feedback.starred ? '★' : '☆'}
          </button>
          <button
            className={`action-btn${feedback.vote === 'up' ? ' voted-up' : ''}`}
            onClick={() =>
              onFeedback({ vote: feedback.vote === 'up' ? null : 'up' })
            }
            aria-label="Thumbs up"
            aria-pressed={feedback.vote === 'up'}
            title="Relevant"
          >
            ↑
          </button>
          <button
            className={`action-btn${feedback.vote === 'down' ? ' voted-down' : ''}`}
            onClick={() =>
              onFeedback({ vote: feedback.vote === 'down' ? null : 'down' })
            }
            aria-label="Thumbs down"
            aria-pressed={feedback.vote === 'down'}
            title="Not relevant"
          >
            ↓
          </button>
          <button
            className="action-btn"
            onClick={() => onFeedback({ hidden: true })}
            aria-label="Hide paper"
            title="Hide"
          >
            ×
          </button>
        </div>
      </div>

      <div className="paper-meta">
        {displayAuthors && (
          <span className="authors">{displayAuthors}</span>
        )}
        {paper.source && (
          <span className="source-badge">{paper.source}</span>
        )}
        {paper.published_at && (
          <span>{formatDate(paper.published_at)}</span>
        )}
        <span className={`score-pill ${scoreClass(paper.score)}`}>
          {Math.round(paper.score * 100)}%
        </span>
      </div>

      {(paper.topics.length > 0 || paper.india) && (
        <div className="paper-chips" role="list" aria-label="Topics">
          {paper.india && (
            <span className="chip india" role="listitem">
              India
            </span>
          )}
          {paper.topics.map((t) => (
            <span key={t} className="chip" role="listitem">
              {t}
            </span>
          ))}
        </div>
      )}

      {paper.abstract && (
        <>
          <button
            className="abstract-toggle"
            onClick={() => setShowAbstract((v) => !v)}
            aria-expanded={showAbstract}
            aria-controls={`abstract-${paper.id}`}
          >
            <span aria-hidden="true">{showAbstract ? '▲' : '▼'}</span>
            {showAbstract ? 'Hide abstract' : 'Show abstract'}
          </button>
          {showAbstract && (
            <div
              className="abstract-text"
              id={`abstract-${paper.id}`}
            >
              {paper.abstract}
            </div>
          )}
        </>
      )}
    </article>
  );
}
