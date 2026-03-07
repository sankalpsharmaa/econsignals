import time
import json
import ssl
import sys
import os
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from abc import ABC, abstractmethod

# macOS Python often lacks system certs; create permissive context as fallback
try:
    _SSL_CTX = ssl.create_default_context()
except ssl.SSLError:
    _SSL_CTX = ssl._create_unverified_context()

# If default context fails cert verification (common on macOS Homebrew Python),
# try certifi, else fall back to unverified
try:
    import certifi
    _SSL_CTX.load_verify_locations(certifi.where())
except (ImportError, Exception):
    # Test if default context works; if not, use unverified
    import urllib.request as _ur
    try:
        _ur.urlopen("https://api.openalex.org/works?per_page=1", timeout=5, context=_SSL_CTX)
    except (ssl.SSLCertVerificationError, ssl.SSLError):
        _SSL_CTX = ssl._create_unverified_context()
    except Exception:
        pass  # network issues are fine, SSL context itself is ok

# Package root and project root
_PKG_ROOT = Path(__file__).resolve().parent.parent
PROJ_ROOT = _PKG_ROOT.parent  # econsignals -> project root


class RateLimiter:
    """Token bucket rate limiter."""

    def __init__(self, requests_per_second: float = 1.0):
        self.min_interval = 1.0 / requests_per_second
        self.last_request = 0.0

    def wait(self):
        elapsed = time.time() - self.last_request
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self.last_request = time.time()


class CircuitBreaker:
    """Simple circuit breaker. Opens after N consecutive failures, resets after cooldown."""

    def __init__(self, failure_threshold: int = 5, cooldown_seconds: int = 300):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.failures = 0
        self.opened_at: float | None = None

    def record_success(self):
        self.failures = 0
        self.opened_at = None

    def record_failure(self):
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.opened_at = time.time()

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if time.time() - self.opened_at > self.cooldown_seconds:
            self.failures = 0
            self.opened_at = None
            return False
        return True


class BaseSensor(ABC):
    """Base class for all EconSignals sensors."""

    name: str = "base"       # Override in subclass
    watch: str = "papers"    # Which watch this sensor belongs to
    rate_limit: float = 1.0  # Requests per second
    max_retries: int = 3
    retry_backoff: float = 2.0  # Exponential backoff multiplier

    def __init__(self):
        self.limiter = RateLimiter(self.rate_limit)
        self.breaker = CircuitBreaker()
        self.stats: dict[str, int | str] = {"found": 0, "new": 0, "errors": 0}
        self._load_env()

    def _load_env(self):
        """Load .env file from project root."""
        env_path = PROJ_ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())

    def fetch_url(self, url: str, headers: dict | None = None, timeout: int = 30) -> bytes:
        """HTTP GET with retry, rate limiting, and circuit breaker.

        Returns response bytes. Raises on final failure.

        Args:
            url: The URL to fetch.
            headers: Optional additional request headers.
            timeout: Socket timeout in seconds.

        Raises:
            RuntimeError: If the circuit breaker is open.
            HTTPError: On a non-retryable HTTP error (4xx, excluding 429).
            URLError | TimeoutError: After all retry attempts are exhausted.
        """
        if self.breaker.is_open:
            raise RuntimeError(f"Circuit breaker open for {self.name}")

        default_headers = {
            "User-Agent": (
                f"EconSignals/1.0 "
                f"(mailto:{os.environ.get('OPENALEX_EMAIL', 'econsignals@example.com')})"
            )
        }
        if headers:
            default_headers.update(headers)

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self.limiter.wait()
            try:
                req = Request(url, headers=default_headers)
                with urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
                    data = resp.read()
                    self.breaker.record_success()
                    return data
            except HTTPError as e:
                last_error = e
                if e.code == 429:  # Rate limited
                    wait = self.retry_backoff ** (attempt + 1) * 2
                    time.sleep(wait)
                elif e.code >= 500:
                    time.sleep(self.retry_backoff ** attempt)
                else:
                    self.breaker.record_failure()
                    raise
            except (URLError, TimeoutError) as e:
                last_error = e
                time.sleep(self.retry_backoff ** attempt)

        self.breaker.record_failure()
        raise last_error  # type: ignore[misc]

    def fetch_json(self, url: str, headers: dict | None = None) -> dict:
        """Fetch URL and parse JSON response.

        Args:
            url: The URL to fetch.
            headers: Optional additional request headers.

        Returns:
            Parsed JSON as a dict.
        """
        data = self.fetch_url(url, headers)
        return json.loads(data)

    @abstractmethod
    def collect(self) -> list[dict]:
        """Run the sensor collection. Return list of paper dicts.

        Each dict should have keys compatible with dedup.ingest_paper():

        Required:
            title (str): Paper title.
            authors (list[str]): Author names.
            source_id (str): Unique ID within this source.

        Optional:
            abstract (str): Paper abstract.
            doi (str): DOI string.
            url (str): Canonical URL.
            published_at (str): ISO date string.
            paper_type (str): One of working_paper, journal_article, report, policy_brief.
            jel_codes (list[str]): JEL classification codes.
            keywords (list[str]): Free-form keywords.
            source_url (str): URL on this source.
            raw_metadata (dict): Full raw API response for this item.
        """
        ...

    def run(self) -> dict:
        """Execute sensor: collect, ingest via dedup, log to DB. Returns stats dict.

        This is the main entry point called by the scan command.
        Outputs a JSON line to stdout so Claude can parse the result.

        Returns:
            A dict with keys: sensor, watch, status, found, new, errors,
            and optionally error_message on failure.
        """
        from econsignals.lib.db import log_sensor_start, log_sensor_end
        from econsignals.lib.dedup import ingest_paper

        run_id = log_sensor_start(self.name, self.watch)

        try:
            items = self.collect()
            self.stats["found"] = len(items)

            for item in items:
                try:
                    source_id = item.pop("source_id")
                    source_url = item.pop("source_url", None)
                    raw_metadata = item.pop("raw_metadata", None)

                    paper_id, is_new = ingest_paper(
                        paper_data=item,
                        source=self.name,
                        source_id=source_id,
                        source_url=source_url,
                        raw_metadata=raw_metadata,
                    )
                    if is_new:
                        self.stats["new"] = int(self.stats["new"]) + 1
                except Exception as e:
                    self.stats["errors"] = int(self.stats["errors"]) + 1
                    print(f"  Error ingesting item: {e}", file=sys.stderr)

            log_sensor_end(run_id, "success", self.stats["found"], self.stats["new"])
        except Exception as e:
            log_sensor_end(run_id, "error", 0, 0, str(e))
            self.stats["error_message"] = str(e)
            print(f"Sensor {self.name} failed: {e}", file=sys.stderr)

        result = {
            "sensor": self.name,
            "watch": self.watch,
            "status": "error" if "error_message" in self.stats else "success",
            **self.stats,
        }
        print(json.dumps(result))
        return result
