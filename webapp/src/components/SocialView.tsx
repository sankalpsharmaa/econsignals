import type { SocialItem } from '../types';
import { formatDate, stripHandle } from '../lib/feed';

interface SocialViewProps {
  items: SocialItem[];
}

export function SocialView({ items }: SocialViewProps) {
  if (items.length === 0) {
    return (
      <div className="state-message">
        <h3>No social items</h3>
        <p>Social sensors may need configuration or API keys.</p>
      </div>
    );
  }

  return (
    <div className="social-list" role="list" aria-label="Social feed">
      {items.map((item, i) => (
        <article
          key={i}
          className="social-card"
          role="listitem"
        >
          <div className="social-header">
            <span className="social-handle">{stripHandle(item.handle)}</span>
            <div className="social-right">
              <span
                className={`platform-badge ${item.source}`}
                aria-label={`Source: ${item.source}`}
              >
                {item.source}
              </span>
              {item.published_at && (
                <span className="social-date" aria-label={`Posted ${formatDate(item.published_at)}`}>
                  {formatDate(item.published_at)}
                </span>
              )}
            </div>
          </div>
          <p className="social-content">{item.content}</p>
          {item.url && (
            <a
              href={item.url.startsWith('http') ? item.url : `https://${item.url}`}
              className="social-link"
              target="_blank"
              rel="noopener noreferrer"
              aria-label="View post"
            >
              View post →
            </a>
          )}
        </article>
      ))}
    </div>
  );
}
