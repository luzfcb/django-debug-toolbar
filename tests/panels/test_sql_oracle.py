import unittest
import uuid

from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection
from django.test import SimpleTestCase, TestCase

from debug_toolbar.panels.sql.forms import SQLSelectForm
from debug_toolbar.panels.sql.oracle_helper import OracleExplainPlanHelper
from tests.models import OraclePlanAuthor, OraclePlanBook


def _make_explain_form(raw_sql, params=None, duration=1.0):
    """
    Utility factory helper to construct SQLSelectForm with pre-filled cleaned_data
    to minimize redundant boilerplate across test cases.
    """
    form = SQLSelectForm(data={})
    form.cleaned_data = {
        "request_id": "test_request",
        "djdt_query_id": "test_query",
        "alias": "default",
        "query": {
            "raw_sql": raw_sql,
            "params": params or [],
            "vendor": "oracle",
            "sql": raw_sql,
            "duration": duration,
            "alias": "default",
        },
    }
    return form


@unittest.skipUnless(connection.vendor == "oracle", "Test valid only on Oracle")
class OracleExplainTestCase(TestCase):
    """
    Tests the refined Oracle explain plan support in SQLSelectForm.
    """

    def test_oracle_explain_success(self):
        User = get_user_model()
        User.objects.get_or_create(username="explain_test_user")

        form = _make_explain_form(
            raw_sql="SELECT * FROM auth_user WHERE username = %s",
            params=["explain_test_user"],
        )

        result, headers = form.explain()

        self.assertEqual(headers, ["PLAN_TABLE_OUTPUT"])

        flat_result = [row[0] for row in result if row and row[0] is not None]

        self.assertTrue(
            any(
                "Plan hash value" in line or "Id" in line or "PLAN_TABLE_OUTPUT" in line
                for line in flat_result
            ),
            f"Expected DBMS_XPLAN structure not found in: {flat_result}",
        )

        self.assertTrue(
            any("Oracle Table and Index Statistics" in line for line in flat_result)
        )
        self.assertTrue(any("Table Statistics:" in line for line in flat_result))


class OracleExplainPlanHelperUnitTestCase(SimpleTestCase):
    """
    Pure unit tests for OracleExplainPlanHelper independent logic.
    These tests are database-independent and run natively on any environment (e.g. SQLite).
    """

    def test_default_display_format_is_all(self):
        self.assertEqual(OracleExplainPlanHelper.display_format, "ALL")

    def test_fetch_raw_plan_uses_all_display_format(self):
        class RecordingCursor:
            def __init__(self):
                self.calls = []

            def execute(self, sql, params=None):
                self.calls.append((sql, params))

            def fetchall(self):
                return [("Plan line",)]

        cursor = RecordingCursor()
        helper = OracleExplainPlanHelper(cursor)

        result = helper._fetch_raw_plan("SELECT * FROM employees", [], "stmt")

        self.assertEqual(result, [("Plan line",)])
        self.assertEqual(len(cursor.calls), 2)
        display_sql, display_params = cursor.calls[1]
        self.assertIn("dbms_xplan.display", display_sql)
        self.assertIn("'ALL'", display_sql)
        self.assertEqual(display_params, ["stmt"])

    def test_execute_suppresses_qbr_before_appending_catalog_statistics_for_legacy_oracle(
        self,
    ):
        class LegacyOracleHelper(OracleExplainPlanHelper):
            @property
            def _oracle_version(self):
                return (19,)

            def _fetch_raw_plan(self, sql, params, stmt_id):
                self.events = ["fetch_raw_plan"]
                return [
                    ("Plan line",),
                    ("Query Block Registry:",),
                    ("---------------------",),
                    ("",),
                    ('  <q o="19"><n>SEL$1</n></q>',),
                ]

            def _get_involved_tables(self, stmt_id):
                self.events.append("get_involved_tables")
                return {("SCOTT", "EMPLOYEES")}

            def _fetch_catalog_statistics(self, table_keys):
                self.events.append("fetch_catalog_statistics")
                return (
                    [("SCOTT", "EMPLOYEES", 500, 10, 50, "2026-08-07 12:00:00")],
                    [
                        (
                            "SCOTT",
                            "EMPLOYEES_IDX",
                            "EMPLOYEES",
                            "NONUNIQUE",
                            "VALID",
                            None,
                        )
                    ],
                )

            def _cleanup_plan_table(self, stmt_id):
                self.events.append("cleanup_plan_table")

        helper = LegacyOracleHelper(cursor=None)

        result, headers = helper.execute("SELECT * FROM employees", [])

        flat_result = [row[0] for row in result]
        self.assertEqual(headers, ["PLAN_TABLE_OUTPUT"])
        self.assertNotIn("Query Block Registry:", flat_result)
        self.assertTrue(
            any("Oracle Table and Index Statistics" in line for line in flat_result)
        )
        self.assertEqual(
            helper.events,
            [
                "fetch_raw_plan",
                "get_involved_tables",
                "fetch_catalog_statistics",
                "cleanup_plan_table",
            ],
        )

    def test_execute_skips_catalog_audit_on_database_error(self):
        class CatalogErrorHelper(OracleExplainPlanHelper):
            def __init__(self):
                super().__init__(cursor=None)
                self.cleaned_up = False

            def _fetch_raw_plan(self, sql, params, stmt_id):
                return [("Plan line 1",), ("Plan line 2",)]

            def _get_involved_tables(self, stmt_id):
                raise DatabaseError("Access denied")

            def _cleanup_plan_table(self, stmt_id):
                self.cleaned_up = True

        helper = CatalogErrorHelper()

        with self.assertLogs(
            "debug_toolbar.panels.sql.oracle_helper", level="WARNING"
        ) as cm:
            result, headers = helper.execute("SELECT * FROM employees", [])

        self.assertEqual(headers, ["PLAN_TABLE_OUTPUT"])
        self.assertEqual(result, [("Plan line 1",), ("Plan line 2",)])
        self.assertTrue(helper.cleaned_up)
        self.assertTrue(any("Oracle catalog audit skipped" in log for log in cm.output))

    def test_execute_respects_disabled_catalog_audit(self):
        class NoAuditHelper(OracleExplainPlanHelper):
            include_catalog_audit = False

            def __init__(self):
                super().__init__(cursor=None)
                self.catalog_was_called = False

            def _fetch_raw_plan(self, sql, params, stmt_id):
                return [("Plan line",)]

            def _get_involved_tables(self, stmt_id):
                self.catalog_was_called = True
                return set()

            def _cleanup_plan_table(self, stmt_id):
                pass

        helper = NoAuditHelper()

        result, headers = helper.execute("SELECT * FROM employees", [])

        self.assertEqual(headers, ["PLAN_TABLE_OUTPUT"])
        self.assertEqual(result, [("Plan line",)])
        self.assertFalse(helper.catalog_was_called)

    def test_cleanup_plan_table_logs_database_error(self):
        class FailingCleanupCursor:
            def execute(self, sql, params=None):
                raise DatabaseError("DELETE failed")

        helper = OracleExplainPlanHelper(FailingCleanupCursor())

        with self.assertLogs(
            "debug_toolbar.panels.sql.oracle_helper", level="WARNING"
        ) as cm:
            helper._cleanup_plan_table("stmt")

        self.assertTrue(
            any("Failed to clean up PLAN_TABLE" in log for log in cm.output)
        )

    def test_suppress_qbr_with_audit_present(self):

        helper = OracleExplainPlanHelper(cursor=None)
        raw_result = [
            ("SELECT * FROM employees",),
            ("Query Block Registry:",),
            ("---------------------",),
            ('  <q o="19"><n>SEL$1</n></q>',),
            ("Oracle Table and Index Statistics (ALL_TABLES & ALL_INDEXES)",),
            ("Table Statistics:",),
        ]

        filtered = helper._suppress_qbr(raw_result)
        flat_filtered = [row[0] for row in filtered]

        self.assertEqual(
            flat_filtered,
            [
                "SELECT * FROM employees",
                "Oracle Table and Index Statistics (ALL_TABLES & ALL_INDEXES)",
                "Table Statistics:",
            ],
        )

    def test_suppress_qbr_without_audit_present(self):

        helper = OracleExplainPlanHelper(cursor=None)
        raw_result = [
            ("SELECT * FROM employees",),
            ("Query Block Registry:",),
            ("---------------------",),
            ('  <q o="19"><n>SEL$1</n></q>',),
        ]

        filtered = helper._suppress_qbr(raw_result)
        flat_filtered = [row[0] for row in filtered]

        self.assertEqual(flat_filtered, ["SELECT * FROM employees"])

    def test_suppress_qbr_no_qbr_present(self):

        helper = OracleExplainPlanHelper(cursor=None)
        raw_result = [
            ("SELECT * FROM employees",),
            ("Plan line 2",),
        ]

        filtered = helper._suppress_qbr(raw_result)
        self.assertEqual(filtered, raw_result)

    def test_suppress_qbr_with_note_and_audit_present(self):

        helper = OracleExplainPlanHelper(cursor=None)
        raw_result = [
            ("SELECT * FROM employees",),
            ("Note",),
            ("-----",),
            ("  - dynamic sampling used",),
            ("Query Block Registry:",),
            ("---------------------",),
            ('  <q o="19"><n>SEL$1</n></q>',),
            ("Oracle Table and Index Statistics (ALL_TABLES & ALL_INDEXES)",),
            ("Table Statistics:",),
        ]

        filtered = helper._suppress_qbr(raw_result)
        flat_filtered = [row[0] for row in filtered]

        self.assertEqual(
            flat_filtered,
            [
                "SELECT * FROM employees",
                "Note",
                "-----",
                "  - dynamic sampling used",
                "Oracle Table and Index Statistics (ALL_TABLES & ALL_INDEXES)",
                "Table Statistics:",
            ],
        )

    def test_render_stats_as_ascii_empty_inputs(self):

        helper = OracleExplainPlanHelper(cursor=None)
        lines = helper._render_stats_as_ascii([], [])
        self.assertEqual(lines, [])

    def test_get_involved_tables_without_cursor_connection_metadata(self):
        """
        Cover fallback paths for cursors that don't expose the wrapped driver
        connection metadata and for index mappings without table names. Django's
        Oracle cursor exposes metadata, but the helper is defensive.
        """

        class CursorWithoutConnectionMetadata:
            cursor = object()

            def __init__(self):
                self.calls = 0

            def execute(self, sql, params):
                self.calls += 1

            def fetchall(self):
                if self.calls == 1:
                    return [("DEFAULT_TEST", "AUTH_USER"), (None, None)]
                if self.calls == 2:
                    return [("DEFAULT_TEST", "AUTH_USER_USERNAME_IDX")]
                return [("DEFAULT_TEST", None)]

        helper = OracleExplainPlanHelper(CursorWithoutConnectionMetadata())
        self.assertEqual(
            helper._get_involved_tables("stmt"), {("DEFAULT_TEST", "AUTH_USER")}
        )


@unittest.skipUnless(connection.vendor == "oracle", "Test valid only on Oracle")
class OracleExplainPlanHelperDBTestCase(TestCase):
    """
    Database-dependent integration tests for OracleExplainPlanHelper.
    Requires a running Oracle database.
    """

    @classmethod
    def setUpTestData(cls):
        authors = [
            OraclePlanAuthor.objects.create(name=f"Author {i}", region=f"r{i % 5}")
            for i in range(20)
        ]
        books = []
        for i in range(300):
            books.append(
                OraclePlanBook(
                    author=authors[i % len(authors)],
                    code=f"code-{i:04d}",
                    category=f"c{i % 10}",
                    rating=i % 100,
                    title=(f"Book {i:04d}" if i % 7 else f"Special Mixed Case {i:04d}"),
                    notes="x" * (20 + (i % 30)),
                    published=i % 3 != 0,
                )
            )
        OraclePlanBook.objects.bulk_create(books)

    def explain_sql(self, cursor, sql, params=None):
        stmt_id = f"dt_test_{uuid.uuid4().hex[:20]}"
        cursor.execute("DELETE FROM PLAN_TABLE WHERE statement_id = %s", [stmt_id])
        try:
            cursor.execute(
                f"EXPLAIN PLAN SET STATEMENT_ID = '{stmt_id}' FOR {sql}",
                params or [],
            )
        except Exception:
            cursor.execute("DELETE FROM PLAN_TABLE WHERE statement_id = %s", [stmt_id])
            raise
        return stmt_id

    def fetch_plan_rows(self, cursor, stmt_id):
        cursor.execute(
            """
            SELECT id, parent_id, operation, options, object_owner,
                   object_name, object_type
            FROM PLAN_TABLE
            WHERE statement_id = %s
            ORDER BY id
            """,
            [stmt_id],
        )
        return cursor.fetchall()

    def cleanup_stmt(self, cursor, stmt_id):
        cursor.execute("DELETE FROM PLAN_TABLE WHERE statement_id = %s", [stmt_id])

    def get_current_user(self, cursor):
        cursor.execute("SELECT USER FROM dual")
        return cursor.fetchone()[0]

    def get_code_unique_index_name(self, cursor):
        cursor.execute(
            """
            SELECT i.index_name
            FROM all_indexes i
            JOIN all_ind_columns c
              ON c.index_owner = i.owner
             AND c.index_name = i.index_name
            WHERE i.table_owner = USER
              AND i.table_name = %s
              AND i.uniqueness = 'UNIQUE'
              AND c.column_name = 'CODE'
              AND ROWNUM = 1
            """,
            [OraclePlanBook._meta.db_table.upper()],
        )
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        return row[0]

    def assert_plan_has_index_operation(self, rows):
        self.assertTrue(
            any(row[2] == "INDEX" for row in rows),
            f"Expected INDEX operation in PLAN_TABLE rows: {rows}",
        )

    def test_real_unique_index_only_plan_reverse_maps_to_table(self):
        with connection.cursor() as cursor:
            current_user = self.get_current_user(cursor)
            table_name = OraclePlanBook._meta.db_table.upper()
            index_name = self.get_code_unique_index_name(cursor)
            stmt_id = self.explain_sql(
                cursor,
                f"""
                SELECT /*+ INDEX(t {index_name}) */ t.code
                FROM {OraclePlanBook._meta.db_table} t
                WHERE t.code = %s
                """,
                ["code-0001"],
            )
            try:
                rows = self.fetch_plan_rows(cursor, stmt_id)
                self.assert_plan_has_index_operation(rows)

                helper = OracleExplainPlanHelper(cursor)
                table_keys = helper._get_involved_tables(stmt_id)

                self.assertIn((current_user, table_name), table_keys)
            finally:
                self.cleanup_stmt(cursor, stmt_id)

    def test_real_nonunique_index_plan_reverse_maps_to_table(self):
        with connection.cursor() as cursor:
            current_user = self.get_current_user(cursor)
            table_name = OraclePlanBook._meta.db_table.upper()
            stmt_id = self.explain_sql(
                cursor,
                f"""
                SELECT /*+ INDEX(t dt_opb_cat_rating) */ t.category, t.rating
                FROM {OraclePlanBook._meta.db_table} t
                WHERE t.category = %s AND t.rating = %s
                """,
                ["c3", 3],
            )
            try:
                rows = self.fetch_plan_rows(cursor, stmt_id)
                self.assert_plan_has_index_operation(rows)

                helper = OracleExplainPlanHelper(cursor)
                table_keys = helper._get_involved_tables(stmt_id)

                self.assertIn((current_user, table_name), table_keys)
            finally:
                self.cleanup_stmt(cursor, stmt_id)

    def test_real_full_table_access_maps_to_table(self):
        with connection.cursor() as cursor:
            current_user = self.get_current_user(cursor)
            table_name = OraclePlanBook._meta.db_table.upper()
            stmt_id = self.explain_sql(
                cursor,
                f"""
                SELECT /*+ FULL(t) */ t.id, t.notes
                FROM {OraclePlanBook._meta.db_table} t
                WHERE t.notes LIKE %s
                """,
                ["x%"],
            )
            try:
                rows = self.fetch_plan_rows(cursor, stmt_id)
                self.assertTrue(
                    any(
                        row[2] == "TABLE ACCESS" and row[5] == table_name
                        for row in rows
                    ),
                    f"Expected TABLE ACCESS for {table_name}: {rows}",
                )

                helper = OracleExplainPlanHelper(cursor)
                table_keys = helper._get_involved_tables(stmt_id)

                self.assertIn((current_user, table_name), table_keys)
            finally:
                self.cleanup_stmt(cursor, stmt_id)

    def test_real_catalog_statistics_include_model_indexes(self):
        with connection.cursor() as cursor:
            current_user = self.get_current_user(cursor)
            table_name = OraclePlanBook._meta.db_table.upper()

            helper = OracleExplainPlanHelper(cursor)
            table_stats, index_stats = helper._fetch_catalog_statistics(
                {(current_user, table_name)}
            )

            self.assertTrue(
                any(row[1] == table_name for row in table_stats),
                f"Expected table stats for {table_name}: {table_stats}",
            )
            index_names = {row[1] for row in index_stats}
            self.assertIn("DT_OPB_CAT_RATING", index_names)
            self.assertIn("DT_OPB_CAT_RAT_DESC", index_names)
            self.assertIn("DT_OPB_PUB_CAT", index_names)
            self.assertIn("DT_OPB_LOWER_TITLE", index_names)
            self.assertTrue(
                all(row[2] == table_name for row in index_stats),
                f"Expected all index stats to belong to {table_name}: {index_stats}",
            )

    def test_full_helper_output_includes_plan_and_catalog_statistics(self):
        table_name = OraclePlanBook._meta.db_table.upper()
        form = _make_explain_form(
            raw_sql=(
                f"SELECT /*+ INDEX(b dt_opb_cat_rating) */ b.id, b.title, b.rating "
                f"FROM {OraclePlanBook._meta.db_table} b "
                "WHERE b.category = %s AND b.rating = %s"
            ),
            params=["c3", 3],
        )

        result, headers = form.explain()

        self.assertEqual(headers, ["PLAN_TABLE_OUTPUT"])
        flat_result = [row[0] for row in result if row and row[0] is not None]
        self.assertTrue(
            any(
                "Plan hash value" in line or "| Id" in line or "Operation" in line
                for line in flat_result
            ),
            f"Expected DBMS_XPLAN output in: {flat_result}",
        )
        self.assertTrue(any(table_name in line for line in flat_result))
        self.assertTrue(any("DT_OPB_CAT_RATING" in line for line in flat_result))
        self.assertTrue(
            any("Oracle Table and Index Statistics" in line for line in flat_result)
        )
        for expected in [
            "Num Rows",
            "Blocks",
            "Avg Row Len",
            "Last Analyzed",
            "Uniqueness",
            "Status",
        ]:
            self.assertTrue(
                any(expected in line for line in flat_result),
                f"Expected {expected!r} in helper output: {flat_result}",
            )
