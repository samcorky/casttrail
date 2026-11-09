"""Container health check: succeeds only if the app answers /health with 200.

A degraded app (it started but cannot serve, see docs/operations.md) answers 503, so
the container is reported unhealthy and the reason is in its log.
"""

import sys
import urllib.error
import urllib.request

URL = "http://127.0.0.1:8000/health"


def main() -> int:
    """Ask the app if it is well.

    Returns:
        0 if it answered 200, otherwise 1.
    """
    try:
        with urllib.request.urlopen(URL, timeout=4) as response:
            return 0 if response.status == 200 else 1
    except urllib.error.URLError, OSError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
