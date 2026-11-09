"""Minimal Redis client (RESP2 over a socket): replaces redis-cli so no OS needs extra tools."""

import socket


class RedisError(Exception):
    pass


class Redis:
    def __init__(self, host="localhost", port=6379, password=None, timeout=5.0):
        self.host, self.port, self.password, self.timeout = host, int(port), password, timeout

    def call(self, *args):
        # ponytail: one connection per command; add pooling if a hot loop ever needs it
        with socket.create_connection((self.host, self.port), self.timeout) as s:
            f = s.makefile("rwb")
            if self.password:
                self._roundtrip(f, "AUTH", self.password)
            return self._roundtrip(f, *args)

    def _roundtrip(self, f, *args):
        f.write(b"*%d\r\n" % len(args))
        for a in args:
            b = a if isinstance(a, bytes) else str(a).encode("utf-8")
            f.write(b"$%d\r\n%s\r\n" % (len(b), b))
        f.flush()
        return self._read(f)

    def _read(self, f):
        line = f.readline()
        if not line:
            raise RedisError("connection closed")
        kind, rest = line[:1], line[1:-2]
        if kind == b"+":
            return rest.decode()
        if kind == b"-":
            raise RedisError(rest.decode())
        if kind == b":":
            return int(rest)
        if kind == b"$":
            n = int(rest)
            return None if n < 0 else f.read(n + 2)[:-2].decode("utf-8", "replace")
        if kind == b"*":
            n = int(rest)
            return None if n < 0 else [self._read(f) for _ in range(n)]
        raise RedisError(f"bad reply {line!r}")
