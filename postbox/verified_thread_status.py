"""Read one thread status through a caller supplied verified connection."""

import math
import time


class StatusUnavailable(Exception):
    """The status could not be read within the verified connection lease."""


def read_verified_status(provider, thread_id, *, timeout=5.0):
    """Connect, read once, and revalidate the connection before returning."""
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 < timeout <= 30):
        raise ValueError("bounded timeout required")
    if not isinstance(thread_id, str) or not thread_id.strip() or len(thread_id) > 1024:
        raise ValueError("exact thread identifier required")

    deadline = time.monotonic() + timeout
    connection = None
    try:
        connection, lease = provider.connect_verified(timeout=timeout)
        if time.monotonic() >= deadline or lease.revalidate() is not True:
            raise StatusUnavailable("connection identity changed")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise StatusUnavailable("status deadline expired")
        status = provider.read_thread_status(connection, thread_id, timeout=remaining)
        if time.monotonic() >= deadline or lease.revalidate() is not True:
            raise StatusUnavailable("connection identity changed")
        return status
    except StatusUnavailable:
        raise
    except Exception as exc:
        raise StatusUnavailable("verified status unavailable") from exc
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
