"""db.py helpers, tested with stand-in connection objects (no database needed)."""
import pytest

import db


class FakeConn:
    def __init__(self):
        self.calls = []

    def commit(self):
        self.calls.append("commit")

    def rollback(self):
        self.calls.append("rollback")


def test_transaction_commits_on_success():
    conn = FakeConn()
    with db.transaction(conn):
        pass
    assert conn.calls == ["commit"]


def test_transaction_rolls_back_and_reraises():
    conn = FakeConn()
    with pytest.raises(ValueError), db.transaction(conn):
        raise ValueError("boom")
    assert conn.calls == ["rollback"]


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class MethodCursor:
    """mysql-connector <= 9: stored_results() is a method."""
    closed = False

    def callproc(self, name, args):
        self.called = (name, args)

    def stored_results(self):
        return iter([Result([{"a": 1}]), Result([{"b": 2}])])

    def close(self):
        self.closed = True


class PropertyCursor(MethodCursor):
    """Announced future form: stored_results is a property."""
    @property
    def stored_results(self):
        return iter([Result([{"a": 1}])])


@pytest.mark.parametrize("cursor_cls", [MethodCursor, PropertyCursor])
def test_call_procedure_returns_the_first_result_set(cursor_cls):
    cur = cursor_cls()
    conn = type("C", (), {"cursor": lambda self, dictionary=False: cur})()
    assert db.call_procedure(conn, "sp_x", ("2026-2027",)) == [{"a": 1}]
    assert cur.called == ("sp_x", ["2026-2027"]) and cur.closed


def test_connect_requires_a_user(monkeypatch):
    monkeypatch.delenv("MYSQL_USER", raising=False)
    with pytest.raises(RuntimeError, match="MYSQL_USER"):
        db.connect()
