"""Two databases behind one seam, so the episode's central claim is checkable.

The claim is that identical application code, at an identically named isolation
level, behaves differently on Postgres and MySQL. That is only worth making if
the code really is identical, so the scenarios in main.py are written once
against this interface and never branch on the engine.

Statements are written with `?` placeholders and translated per dialect: `$1,
$2, ...` for asyncpg, `%s` for aiomysql. That translation is the ONLY difference
between what the two engines are sent, which is why `sql_for()` is exported --
the capture prints both renderings side by side so a viewer can confirm it with
their eyes rather than take it on trust.
"""
import re
from typing import Any, Protocol

import aiomysql
import asyncpg

ISOLATION = {
    "read-committed": "READ COMMITTED",
    "repeatable-read": "REPEATABLE READ",
    "serializable": "SERIALIZABLE",
}


def sql_for(engine: str, sql: str) -> str:
    """`?` placeholders rendered for one dialect. The whole of the difference."""
    if engine == "mysql":
        return sql.replace("?", "%s")
    n = 0

    def sub(_m: re.Match) -> str:
        nonlocal n
        n += 1
        return f"${n}"

    return re.sub(r"\?", sub, sql)


class Outcome:
    """What happened to one request, in engine-neutral terms.

    `code` is the driver's own error identifier -- SQLSTATE on Postgres, errno on
    MySQL -- and is never normalised across the two. The episode is about the
    engines disagreeing; flattening their error codes into a shared vocabulary
    would hide exactly the thing being measured.
    """

    __slots__ = ("status", "code", "detail")

    def __init__(self, status: str, code: str = "", detail: str = "") -> None:
        self.status = status
        self.code = code
        self.detail = detail


class Cx(Protocol):
    """A connection inside a transaction, at a chosen isolation level."""

    async def fetchrow(self, sql: str, *args: Any) -> dict | None: ...
    async def fetchall(self, sql: str, *args: Any) -> list[dict]: ...
    async def execute(self, sql: str, *args: Any) -> int: ...


class PgCx:
    def __init__(self, con: asyncpg.Connection) -> None:
        self._con = con

    async def fetchrow(self, sql: str, *args):
        row = await self._con.fetchrow(sql_for("postgres", sql), *args)
        return dict(row) if row is not None else None

    async def fetchall(self, sql: str, *args):
        rows = await self._con.fetch(sql_for("postgres", sql), *args)
        return [dict(r) for r in rows]

    async def execute(self, sql: str, *args) -> int:
        tag = await self._con.execute(sql_for("postgres", sql), *args)
        # "UPDATE 1" / "INSERT 0 1" -> the row count, which is how both the
        # optimistic guard and the WHERE-clause fix find out they lost.
        parts = tag.split()
        return int(parts[-1]) if parts and parts[-1].isdigit() else 0


class MyCx:
    def __init__(self, cur: aiomysql.DictCursor) -> None:
        self._cur = cur

    async def fetchrow(self, sql: str, *args):
        await self._cur.execute(sql_for("mysql", sql), args)
        return await self._cur.fetchone()

    async def fetchall(self, sql: str, *args):
        await self._cur.execute(sql_for("mysql", sql), args)
        return list(await self._cur.fetchall())

    async def execute(self, sql: str, *args) -> int:
        return await self._cur.execute(sql_for("mysql", sql), args)


class Postgres:
    name = "postgres"

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def version(self) -> str:
        async with self._pool.acquire() as con:
            return (await con.fetchval("SELECT version()")).split(",")[0]

    async def run(self, scenario, req, isolation: str) -> Outcome:
        level = ISOLATION[isolation]
        try:
            async with self._pool.acquire() as con:
                tx = con.transaction(isolation=isolation.replace("-", "_"))
                await tx.start()
                try:
                    out = await scenario(PgCx(con), req)
                    await tx.commit()
                    return out
                except BaseException:
                    await tx.rollback()
                    raise
        except asyncpg.SerializationError as e:
            return Outcome("aborted", e.sqlstate or "40001", "serialization failure")
        except asyncpg.DeadlockDetectedError as e:
            return Outcome("aborted", e.sqlstate or "40P01", "deadlock detected")
        except asyncpg.CheckViolationError as e:
            return Outcome("check_violation", e.sqlstate or "23514", str(level))
        except asyncpg.PostgresError as e:
            return Outcome("error", getattr(e, "sqlstate", "") or "?", type(e).__name__)


class MySQL:
    name = "mysql"

    def __init__(self, pool: aiomysql.Pool) -> None:
        self._pool = pool

    async def version(self) -> str:
        async with self._pool.acquire() as con:
            async with con.cursor() as cur:
                await cur.execute("SELECT version()")
                return "MySQL " + (await cur.fetchone())[0]

    async def run(self, scenario, req, isolation: str) -> Outcome:
        level = ISOLATION[isolation]
        try:
            async with self._pool.acquire() as con:
                async with con.cursor(aiomysql.DictCursor) as cur:
                    # Explicit, in this order, and it matters.
                    #
                    # With autocommit off, aiomysql opens a transaction on the
                    # FIRST statement -- which means `SET TRANSACTION ISOLATION
                    # LEVEL` lands inside one, applies only to the next
                    # transaction, and the following `begin()` implicitly
                    # commits the one it was sitting in. The level silently did
                    # not apply, and requests hung waiting on locks held by
                    # transactions nobody had closed.
                    #
                    # The pool runs with autocommit ON and transactions are
                    # opened by hand, so this reads exactly like what you would
                    # type into a mysql client, and there is no hidden
                    # transaction for the setting to fall into.
                    await cur.execute(f"SET SESSION TRANSACTION ISOLATION LEVEL {level}")
                    await cur.execute("BEGIN")
                    try:
                        out = await scenario(MyCx(cur), req)
                        await cur.execute("COMMIT")
                        return out
                    except BaseException:
                        await cur.execute("ROLLBACK")
                        raise
        except aiomysql.Error as e:
            errno = e.args[0] if e.args else 0
            if errno == 1213:
                return Outcome("aborted", "1213", "deadlock found")
            if errno == 1205:
                return Outcome("aborted", "1205", "lock wait timeout")
            if errno == 3819:
                return Outcome("check_violation", "3819", "check constraint violated")
            return Outcome("error", str(errno), type(e).__name__)
