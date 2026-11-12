"""Fake Redis (RESP over TCP) so the suite needs no Redis, Docker or network."""

import socketserver
import threading

import pytest

from flow_engine import engine


class _Handler(socketserver.StreamRequestHandler):
    def _cmd(self):
        head = self.rfile.readline()
        if not head:
            return None
        args = []
        for _ in range(int(head[1:])):
            n = int(self.rfile.readline()[1:])
            args.append(self.rfile.read(n + 2)[:-2].decode())
        return args

    def handle(self):
        db, pw = self.server.db, self.server.password
        authed = pw is None
        while (a := self._cmd()) is not None:
            c = a[0].upper()
            if c == "AUTH":
                authed = a[1] == pw
                out = b"+OK\r\n" if authed else b"-ERR invalid password\r\n"
            elif not authed:
                out = b"-NOAUTH Authentication required.\r\n"
            elif c == "PING":
                out = b"+PONG\r\n"
            elif c == "SET":
                db[a[1]] = a[2]
                out = b"+OK\r\n"
            elif c == "GET":
                v = db.get(a[1])
                out = b"$-1\r\n" if v is None else b"$%d\r\n%s\r\n" % (len(v.encode()), v.encode())
            elif c == "EXISTS":
                out = b":%d\r\n" % (a[1] in db)
            elif c == "HSET":
                db.setdefault(a[1], {}).update(dict(zip(a[2::2], a[3::2])))
                out = b":1\r\n"
            elif c == "HGETALL":
                flat = [x for kv in db.get(a[1], {}).items() for x in kv]
                out = b"*%d\r\n" % len(flat) + b"".join(b"$%d\r\n%s\r\n" % (len(x.encode()), x.encode()) for x in flat)
            elif c == "EXPIRE":
                out = b":1\r\n"
            else:
                out = b"-ERR unknown command\r\n"
            self.wfile.write(out)


@pytest.fixture(autouse=True)
def isolated_agent_memory(monkeypatch, tmp_path_factory):
    """Every test gets its own agent-system DB; the user's real memory is never touched."""
    db_dir = tmp_path_factory.mktemp("agent_db")
    monkeypatch.setenv("AGENT_SYSTEM_DB_DIR", str(db_dir))
    return db_dir


@pytest.fixture(autouse=True)
def isolated_flow_log(monkeypatch, tmp_path_factory):
    """Flow log lines (engine.log, API call lines) go to a temp dir, never the repo's logs/."""
    logs = tmp_path_factory.mktemp("flow_logs")
    monkeypatch.setattr(engine, "LOGS", logs)
    monkeypatch.setattr(engine, "LOG", logs / "flow_engine.log")
    return logs


@pytest.fixture
def fake_redis(monkeypatch, tmp_path):
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads, srv.db, srv.password = True, {}, "s3cret"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("REDIS_HOST", "127.0.0.1")
    monkeypatch.setenv("REDIS_PORT", str(srv.server_address[1]))
    monkeypatch.setenv("REDIS_PASSWORD", "s3cret")
    monkeypatch.setattr(engine, "ROOT", tmp_path)
    monkeypatch.setattr(engine, "INCOMING", tmp_path / "incoming_tasks")
    monkeypatch.setattr(engine, "LOGS", tmp_path / "logs")
    monkeypatch.setattr(engine, "LOG", tmp_path / "logs" / "flow_engine.log")
    yield srv
    srv.shutdown()
