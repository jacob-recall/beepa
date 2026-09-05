"""Bounded request reads for trusted-local HTTP helpers (stdlib only)."""
import time


class BodyError(ValueError):
    def __init__(self, status):
        self.status = status
        super().__init__('Invalid or incomplete request body')


def read_body(handler, limit, deadline_seconds=5):
    lengths = handler.headers.get_all('Content-Length', [])
    if handler.headers.get('Transfer-Encoding') or len(lengths) > 1:
        raise BodyError(400)
    try:
        size = int(lengths[0]) if lengths else 0
    except ValueError:
        raise BodyError(400) from None
    if size < 0:
        raise BodyError(400)
    if size > limit:
        raise BodyError(413)
    deadline = time.monotonic() + deadline_seconds
    body = bytearray()
    while len(body) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BodyError(408)
        handler.connection.settimeout(remaining)
        try:
            read = getattr(handler.rfile, 'read1', handler.rfile.read)
            chunk = read(min(size - len(body), 65536))
        except (TimeoutError, OSError):
            raise BodyError(408) from None
        if not chunk:
            raise BodyError(400)
        body.extend(chunk)
    return bytes(body)


class BoundedBodyMixin:
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def _bounded_body(self, limit):
        try:
            return read_body(self, limit)
        except BodyError as exc:
            self._body_rejected = True
            self.close_connection = True
            self._json(exc.status, {'error': 'Invalid or incomplete request body.'}, cors=True)
            return None
