import type { Deadline } from '../types';
import { formatDate } from '../lib/feed';

interface DeadlinesViewProps {
  deadlines: Deadline[];
}

const ELIGIBILITY_LABEL: Record<string, string> = {
  phd_student: 'PhD student',
  faculty: 'Faculty',
  both: 'PhD + Faculty',
};

export function DeadlinesView({ deadlines }: DeadlinesViewProps) {
  const sorted = [...deadlines].sort((a, b) => {
    // Dated deadlines first (soonest due wins — urgency matters); rolling
    // calls last, ordered by relevance since they carry no deadline pressure.
    if (a.rolling !== b.rolling) return a.rolling ? 1 : -1;
    if (!a.rolling) return (a.due || '').localeCompare(b.due || '');
    return (b.relevance ?? 0) - (a.relevance ?? 0);
  });

  if (sorted.length === 0) {
    return (
      <div className="state-message">
        <h3>No deadlines</h3>
        <p>Check back after the next sensor run.</p>
      </div>
    );
  }

  return (
    <div className="deadlines-list" role="list" aria-label="Deadlines">
      {sorted.map((d, i) => {
        const elig = d.eligibility ? ELIGIBILITY_LABEL[d.eligibility] ?? d.eligibility : null;
        return (
          <article
            key={i}
            className="deadline-card"
            role="listitem"
            aria-label={d.name}
          >
            <div className={`urgency-bar ${d.urgency}`} aria-hidden="true" />

            <div className="deadline-body">
              <div className="deadline-name">
                {d.url ? (
                  <a href={d.url} target="_blank" rel="noopener noreferrer">
                    {d.name}
                  </a>
                ) : (
                  d.name
                )}
              </div>
              {d.organization && (
                <div className="deadline-org">{d.organization}</div>
              )}
              {(elig || d.amount) && (
                <div className="deadline-meta">
                  {elig && <span className="elig-badge">{elig}</span>}
                  {d.amount && <span className="amount-badge">{d.amount}</span>}
                </div>
              )}
              {d.description && (
                <div className="deadline-description">{d.description}</div>
              )}
            </div>

            <div className="deadline-right">
              <span className={`type-tag ${d.type}`}>
                {d.type === 'cfp' ? 'CfP' : 'Funding'}
              </span>
              {d.due && (
                <span className="deadline-due" aria-label={`Due ${formatDate(d.due)}`}>
                  {formatDate(d.due)}
                </span>
              )}
              <span className={`days-left ${d.urgency}`}>
                {d.rolling
                  ? 'Rolling'
                  : d.days_left != null
                  ? `${d.days_left}d left`
                  : '—'}
              </span>
            </div>
          </article>
        );
      })}
    </div>
  );
}
