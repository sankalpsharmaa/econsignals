import type { Sensor } from '../types';
import { formatDate } from '../lib/feed';

interface StatusViewProps {
  sensors: Sensor[];
  generatedAt: string | null;
}

function statusClass(status: string): string {
  if (status === 'success') return 'success';
  if (status === 'error' || status === 'failed') return 'error';
  if (status === 'partial') return 'warning';
  return 'unknown';
}

export function StatusView({ sensors, generatedAt }: StatusViewProps) {
  return (
    <div>
      <div className="status-section">
        <h2>Sensor Health</h2>
        <div className="status-table-wrap">
          <table className="status-table" aria-label="Sensor health">
            <thead>
              <tr>
                <th scope="col">Sensor</th>
                <th scope="col">Status</th>
                <th scope="col">Last run</th>
                <th scope="col">Items found</th>
              </tr>
            </thead>
            <tbody>
              {sensors.map((s) => (
                <tr key={s.sensor}>
                  <td>{s.sensor}</td>
                  <td>
                    <span
                      className={`status-dot ${statusClass(s.status)}`}
                      aria-label={`Status: ${s.status}`}
                    >
                      {s.status}
                    </span>
                  </td>
                  <td>
                    {s.last_run ? formatDate(s.last_run) : '—'}
                  </td>
                  <td>
                    {s.items_found != null ? s.items_found : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {generatedAt && (
        <p className="generated-info">
          Feed generated: {generatedAt} ({formatDate(generatedAt)})
        </p>
      )}
    </div>
  );
}
