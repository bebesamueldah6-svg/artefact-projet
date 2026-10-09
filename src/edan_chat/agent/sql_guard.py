"""Validation and sandboxed execution of LLM-generated SQL.

Defense in depth:
1. sqlglot parse: exactly one read-only query (SELECT / UNION / CTE).
2. Allowlist: only the curated views (+ national_totals) and CTE names may be referenced, and
   only their columns (or aliases defined in the query itself).
3. Denylist of functions that touch files, network or the environment.
4. Row cap enforced by rewriting the outer LIMIT.
5. Execution on a read-only DuckDB connection with external access disabled and a timeout.

Results are memoised per (database file, mtime, SQL): re-ingesting rewrites the file, which
invalidates the cache.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import duckdb
import pandas as pd
import sqlglot
from sqlglot import exp

from edan_chat import config

ALLOWED_TABLES = {
    "vw_results_clean",
    "vw_winners",
    "vw_turnout",
    "vw_turnout_region",
    "vw_party_summary",
    "national_totals",
}

# base tables / catalogs a CTE must not shadow (it would bypass the allowlist)
_RESERVED_NAMES = {"candidatures", "circonscriptions", "information_schema", "tables", "columns"}
_FORBIDDEN_FUNC_PREFIXES = ("read_", "glob", "getenv", "current_setting", "duckdb_", "pragma_")


@lru_cache(maxsize=4)
def allowed_columns(db_path: str = str(config.DB_PATH)) -> frozenset[str]:
    con = duckdb.connect(db_path, read_only=True)
    try:
        cols = {r[0].lower() for t in ALLOWED_TABLES for r in con.execute(f"DESCRIBE {t}").fetchall()}
    finally:
        con.close()
    return frozenset(cols)


class UnsafeSQLError(ValueError):
    """The generated SQL was rejected by the guard (message is shown to the LLM for repair)."""


@dataclass
class QueryResult:
    sql: str            # SQL actually executed (after rewriting)
    df: pd.DataFrame
    truncated: bool     # more rows than SQL_MAX_ROWS existed


def validate(sql: str, max_rows: int = config.SQL_MAX_ROWS, db_path=config.DB_PATH) -> str:
    """Return a safe, row-capped version of `sql` or raise UnsafeSQLError."""
    # small LLMs sometimes leave JSON / markdown debris around the query
    sql = sql.strip().removeprefix("```sql").strip("`").strip().rstrip(";}").strip()
    if not sql:
        raise UnsafeSQLError("Requête vide.")
    try:
        statements = sqlglot.parse(sql, read="duckdb")
    except sqlglot.errors.ParseError as e:
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e))  # drop terminal colour codes
        raise UnsafeSQLError(f"SQL invalide : {msg}") from e
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise UnsafeSQLError("Une seule requête SELECT est autorisée.")
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        raise UnsafeSQLError("Seules les requêtes SELECT sont autorisées.")

    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    shadowing = cte_names & _RESERVED_NAMES
    if shadowing or any(n.startswith(("duckdb_", "sqlite_", "pg_")) for n in cte_names):
        raise UnsafeSQLError(f"Nom de CTE réservé : {', '.join(sorted(shadowing or cte_names))}")
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise UnsafeSQLError("Les fonctions-tables (read_csv, etc.) sont interdites.")
        name = table.name.lower()
        if table.db and table.db.lower() not in ("main", ""):
            raise UnsafeSQLError(f"Schéma non autorisé : {table.db}")
        if name not in ALLOWED_TABLES and name not in cte_names:
            raise UnsafeSQLError(
                f"Table non autorisée : {table.name}. Tables permises : {', '.join(sorted(ALLOWED_TABLES))}"
            )
    defined = ({a.alias.lower() for a in tree.find_all(exp.Alias)}
               | {c.name.lower() for t in tree.find_all(exp.TableAlias) for c in t.columns}
               | cte_names)
    allowed = allowed_columns(str(db_path)) | defined
    for col in tree.find_all(exp.Column):
        if col.name and col.name.lower() not in allowed:
            raise UnsafeSQLError(f"Colonne inconnue ou non autorisée : {col.name}")
    for func in tree.find_all(exp.Func):
        fname = (func.sql_name() if not isinstance(func, exp.Anonymous) else func.name).lower()
        if fname.startswith(_FORBIDDEN_FUNC_PREFIXES):
            raise UnsafeSQLError(f"Fonction interdite : {fname}")

    limit = tree.args.get("limit")
    current = None
    if limit is not None:
        try:
            current = int(limit.expression.name)
        except (AttributeError, ValueError):
            current = None
    if current is None or current > max_rows:
        # fetch one extra row to detect truncation
        tree = tree.limit(max_rows + 1)
    return tree.sql(dialect="duckdb")


def execute(
    sql: str,
    db_path=config.DB_PATH,
    max_rows: int = config.SQL_MAX_ROWS,
    timeout_s: float = config.SQL_TIMEOUT_S,
) -> QueryResult:
    safe_sql = validate(sql, max_rows, db_path)
    df = _run(str(db_path), Path(db_path).stat().st_mtime_ns, safe_sql, timeout_s).copy()
    truncated = len(df) > max_rows
    return QueryResult(sql=safe_sql, df=df.head(max_rows), truncated=truncated)


@lru_cache(maxsize=256)
def _run(db_path: str, _mtime: int, safe_sql: str, timeout_s: float) -> pd.DataFrame:
    con = duckdb.connect(db_path, read_only=True, config={"enable_external_access": False})
    timer = threading.Timer(timeout_s, con.interrupt)
    timer.start()
    try:
        return con.execute(safe_sql).df()
    except duckdb.InterruptException as e:
        raise UnsafeSQLError(f"Requête trop longue (> {timeout_s:.0f} s).") from e
    finally:
        timer.cancel()
        con.close()
