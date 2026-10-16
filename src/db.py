"""
db.py -- MySQL connection helpers.

All credentials come from environment variables (or a .env file, loaded by
utils.load_env):
    MYSQL_HOST         default 127.0.0.1
    MYSQL_PORT         default 3306
    MYSQL_USER         required
    MYSQL_PASSWORD     required (may be empty for a passwordless local user)
    MYSQL_DATABASE     default university_timetable
    MYSQL_UNIX_SOCKET  optional; if set, connect through this socket instead
                       of TCP (handy for a local MariaDB/MySQL install)
"""
from __future__ import annotations

import os
from contextlib import contextmanager

import mysql.connector

import utils

utils.load_env()


def connect(autocommit: bool = False):
    """Open a new connection. autocommit=False so callers control transactions."""
    user = os.environ.get("MYSQL_USER")
    if not user:
        raise RuntimeError(
            "MYSQL_USER is not set. Copy .env.example to .env and fill it in "
            "(or export the MYSQL_* variables).")
    params = dict(
        user=user,
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ.get("MYSQL_DATABASE", "university_timetable"),
        autocommit=autocommit,
        charset="utf8mb4",
    )
    sock = os.environ.get("MYSQL_UNIX_SOCKET")
    if sock:
        params["unix_socket"] = sock
    else:
        params["host"] = os.environ.get("MYSQL_HOST", "127.0.0.1")
        params["port"] = int(os.environ.get("MYSQL_PORT", "3306"))
    return mysql.connector.connect(**params)


@contextmanager
def connection(autocommit: bool = False):
    """`with connection() as conn:` -- always closes the connection."""
    conn = connect(autocommit=autocommit)
    try:
        yield conn
    finally:
        conn.close()


def query(conn, sql: str, params=None) -> list[dict]:
    """Run a SELECT and return rows as a list of dicts."""
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(sql, params or ())
        return cur.fetchall()
    finally:
        cur.close()


def call_procedure(conn, name: str, args=()) -> list[dict]:
    """
    Call a stored procedure and return the rows of its (first) result set.
    Used for sp_list_clashes, sp_faculty_workload, ...
    """
    cur = conn.cursor(dictionary=True)
    try:
        cur.callproc(name, list(args))
        rows: list[dict] = []
        for result in cur.stored_results():
            rows = result.fetchall()
            break
        return rows
    finally:
        cur.close()


@contextmanager
def transaction(conn):
    """
    `with transaction(conn):` -- commit on success, rollback on any exception.
    The exception is re-raised so the caller can report it.
    """
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
