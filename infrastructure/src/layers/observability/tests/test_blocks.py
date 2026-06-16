"""
Unit tests for block_filter and is_blocked helpers.

Story 6.0b AC:
  block_filter:
    - Returns a SQL fragment of the form:
        NOT EXISTS (SELECT 1 FROM blocks b
          WHERE (b.blocker_id = <col> AND b.blocked_id = %s)
             OR (b.blocker_id = %s AND b.blocked_id = <col>))
    - Accepts only a hard-coded whitelist of column identifiers.
    - Raises ValueError for any column identifier not in the whitelist.
    - Does NOT interpolate user-supplied values — only the whitelisted column name.

  is_blocked:
    - Returns True when a forward block exists (a blocks b).
    - Returns True when a reverse block exists (b blocks a).
    - Returns False when no block exists between the pair.
    - Issues the SELECT via parameter binding (cur.execute(sql, (a, b, b, a))).
"""

import unittest
from unittest.mock import MagicMock, call


# ---------------------------------------------------------------------------
# block_filter tests
# ---------------------------------------------------------------------------


class TestBlockFilterWhitelistAccepted(unittest.TestCase):
    """
    Given a column reference that is in the hard-coded whitelist,
    when block_filter is called,
    then it returns a non-empty SQL fragment string.
    """

    def test_given_friendships_user_a_when_block_filter_then_returns_sql_fragment(self):
        from knotify_obs import block_filter

        result = block_filter("f.user_a")

        self.assertIsInstance(result, str)
        self.assertIn("NOT EXISTS", result)
        self.assertIn("blocks", result)
        self.assertIn("f.user_a", result)

    def test_given_friendships_user_b_when_block_filter_then_returns_sql_fragment(self):
        from knotify_obs import block_filter

        result = block_filter("f.user_b")

        self.assertIsInstance(result, str)
        self.assertIn("NOT EXISTS", result)
        self.assertIn("f.user_b", result)

    def test_given_friend_requests_from_user_id_when_block_filter_then_returns_sql_fragment(self):
        from knotify_obs import block_filter

        result = block_filter("fr.from_user_id")

        self.assertIn("NOT EXISTS", result)
        self.assertIn("fr.from_user_id", result)

    def test_given_friend_requests_to_user_id_when_block_filter_then_returns_sql_fragment(self):
        from knotify_obs import block_filter

        result = block_filter("fr.to_user_id")

        self.assertIn("NOT EXISTS", result)
        self.assertIn("fr.to_user_id", result)

    def test_given_bookmarks_bookmarked_user_id_when_block_filter_then_returns_sql_fragment(self):
        from knotify_obs import block_filter

        result = block_filter("bk.bookmarked_user_id")

        self.assertIn("NOT EXISTS", result)
        self.assertIn("bk.bookmarked_user_id", result)

    def test_given_users_user_id_when_block_filter_then_returns_sql_fragment(self):
        from knotify_obs import block_filter

        result = block_filter("u.user_id")

        self.assertIn("NOT EXISTS", result)
        self.assertIn("u.user_id", result)

    def test_given_deck_view_user_id_when_block_filter_then_returns_sql_fragment(self):
        """
        story 7.2 whitelist entry: deck_view aliased as dv uses dv.user_id in block_filter.
        Added alongside u.user_id in a single _ALLOWED_COLUMNS edit (story 7.1 AC).
        """
        from knotify_obs import block_filter

        result = block_filter("dv.user_id")

        self.assertIsInstance(result, str)
        self.assertIn("NOT EXISTS", result)
        self.assertIn("blocks", result)
        self.assertIn("dv.user_id", result)


class TestBlockFilterWhitelistRejected(unittest.TestCase):
    """
    Given a column reference that is NOT in the whitelist,
    when block_filter is called,
    then ValueError is raised.
    """

    def test_given_injection_attempt_when_block_filter_then_raises_value_error(self):
        from knotify_obs import block_filter

        with self.assertRaises(ValueError):
            block_filter("' OR 1=1 --")

    def test_given_arbitrary_string_when_block_filter_then_raises_value_error(self):
        from knotify_obs import block_filter

        with self.assertRaises(ValueError):
            block_filter("some_table.some_col")

    def test_given_empty_string_when_block_filter_then_raises_value_error(self):
        from knotify_obs import block_filter

        with self.assertRaises(ValueError):
            block_filter("")

    def test_given_partial_whitelist_match_when_block_filter_then_raises_value_error(self):
        from knotify_obs import block_filter

        # "f" alone (without ".user_a") must not be accepted
        with self.assertRaises(ValueError):
            block_filter("f")

    def test_given_whitelist_value_with_extra_suffix_when_block_filter_then_raises_value_error(self):
        from knotify_obs import block_filter

        # Prefix-match of whitelist entry is NOT sufficient
        with self.assertRaises(ValueError):
            block_filter("f.user_a; DROP TABLE users--")

    def test_given_pre_alias_table_qualified_when_block_filter_then_raises_value_error(self):
        from knotify_obs import block_filter

        # The old whitelist used bare table names (e.g., "friendships.user_a"),
        # but those broke correlated subqueries against aliased outer FROM
        # clauses. Reject the old form explicitly so a regression is caught
        # at unit-test time, not at runtime in Aurora.
        for legacy in (
            "friendships.user_a",
            "friendships.user_b",
            "friend_requests.requester_id",
            "friend_requests.receiver_id",
            "bookmarks.bookmarked_user_id",
            "users.user_id",
        ):
            with self.assertRaises(ValueError, msg=f"legacy form {legacy!r} must be rejected"):
                block_filter(legacy)


class TestBlockFilterSqlShape(unittest.TestCase):
    """
    Given a valid whitelisted column reference,
    when block_filter is called,
    then the returned SQL fragment has the exact structure specified in the AC:
      NOT EXISTS (SELECT 1 FROM blocks b
        WHERE (b.blocker_id = <col> AND b.blocked_id = %s)
           OR (b.blocker_id = %s AND b.blocked_id = <col>))
    and contains exactly two %s parameter placeholders.
    """

    def test_given_friendships_user_b_when_block_filter_then_fragment_has_correct_structure(self):
        from knotify_obs import block_filter

        col = "f.user_b"
        fragment = block_filter(col)

        # Both directions of the block check must appear
        self.assertIn(f"b.blocker_id = {col}", fragment)
        self.assertIn(f"b.blocked_id = {col}", fragment)
        # The caller binds two %s params (the requesting user's id, twice)
        self.assertEqual(fragment.count("%s"), 2)
        # Must be a NOT EXISTS form
        self.assertTrue(fragment.strip().startswith("NOT EXISTS"))

    def test_given_users_user_id_when_block_filter_then_fragment_has_correct_structure(self):
        from knotify_obs import block_filter

        col = "u.user_id"
        fragment = block_filter(col)

        self.assertIn(f"b.blocker_id = {col}", fragment)
        self.assertIn(f"b.blocked_id = {col}", fragment)
        self.assertEqual(fragment.count("%s"), 2)
        self.assertTrue(fragment.strip().startswith("NOT EXISTS"))

    def test_given_deck_view_user_id_when_block_filter_then_fragment_has_correct_structure(self):
        """
        story 7.2: dv.user_id whitelist entry produces a correctly-shaped fragment.
        """
        from knotify_obs import block_filter

        col = "dv.user_id"
        fragment = block_filter(col)

        self.assertIn(f"b.blocker_id = {col}", fragment)
        self.assertIn(f"b.blocked_id = {col}", fragment)
        self.assertEqual(fragment.count("%s"), 2)
        self.assertTrue(fragment.strip().startswith("NOT EXISTS"))


class TestBlockFilterEmbeddedInSelect(unittest.TestCase):
    """
    Given the SQL fragment from block_filter("f.user_b"),
    when embedded into a representative SELECT against friendships and
    executed against a real Postgres database with test data,
    then blocked pairs are filtered out in both directions.

    This test uses a real DB connection via docker-compose and is
    therefore only run when the DB container is available.
    """

    def test_given_block_in_either_direction_when_query_with_fragment_then_row_filtered(self):
        """
        Seed a friendships row between user_a and user_b.
        Insert a blocks row for the pair (user_a blocks user_b).
        Execute SELECT ... WHERE <block_filter("f.user_b")>
        Assert the friendship row does NOT appear (blocker_id = user_a → blocked_id = user_b → filter fires).
        Also assert a friendship between user_c and user_d (no block) DOES appear.
        Repeat with the reverse block direction (user_b blocks user_a).
        """
        import os
        import uuid
        import psycopg2
        import psycopg2.extras

        psycopg2.extras.register_uuid()

        host = os.environ.get("PGHOST", "localhost")
        port = int(os.environ.get("PGPORT", "5432"))
        dbname = os.environ.get("PGDATABASE", "knotify")
        master_user = "knotify"
        master_pass = os.environ.get("PGPASSWORD", "knotify")

        try:
            conn = psycopg2.connect(
                host=host, port=port, dbname=dbname,
                user=master_user, password=master_pass
            )
        except psycopg2.OperationalError:
            self.skipTest("docker-compose DB not available")

        conn.autocommit = True

        # Generate UUIDs with deterministic lex ordering so the requesting
        # user (user_a) always lands in the user_a column of friendships. The
        # test below queries `WHERE f.user_a = %s` and binds user_a — without
        # this, a 50/50 UUID coin flip puts user_a on the f.user_b side and
        # the assertion would oscillate per run.
        user_a = uuid.uuid4()
        user_b = uuid.uuid4()
        if str(user_a) > str(user_b):
            user_a, user_b = user_b, user_a
        user_c = uuid.uuid4()
        user_d = uuid.uuid4()
        if str(user_c) > str(user_d):
            user_c, user_d = user_d, user_c
        pair_ab = (str(user_a), str(user_b))
        pair_cd = (str(user_c), str(user_d))

        try:
            with conn.cursor() as cur:
                # Seed users
                for uid, email in [
                    (user_a, f"blk_test_a_{uuid.uuid4().hex[:6]}@test.invalid"),
                    (user_b, f"blk_test_b_{uuid.uuid4().hex[:6]}@test.invalid"),
                    (user_c, f"blk_test_c_{uuid.uuid4().hex[:6]}@test.invalid"),
                    (user_d, f"blk_test_d_{uuid.uuid4().hex[:6]}@test.invalid"),
                ]:
                    cur.execute(
                        "INSERT INTO users (user_id, email) VALUES (%s, %s)", (uid, email)
                    )

                # Seed friendships
                cur.execute(
                    "INSERT INTO friendships (user_a, user_b) VALUES (%s, %s)",
                    (uuid.UUID(pair_ab[0]), uuid.UUID(pair_ab[1])),
                )
                cur.execute(
                    "INSERT INTO friendships (user_a, user_b) VALUES (%s, %s)",
                    (uuid.UUID(pair_cd[0]), uuid.UUID(pair_cd[1])),
                )

                # Seed forward block: user_a blocks user_b
                cur.execute(
                    "INSERT INTO blocks (blocker_id, blocked_id) VALUES (%s, %s)",
                    (user_a, user_b),
                )

            from knotify_obs import block_filter

            fragment = block_filter("f.user_b")

            # Query from the perspective of user_a (requesting user = user_a)
            # The friendships row with user_b in the user_b column should be filtered.
            sql = f"""
                SELECT f.user_a, f.user_b
                FROM friendships f
                WHERE f.user_a = %s
                  AND {fragment}
            """
            with conn.cursor() as cur:
                # Params: user_a (WHERE f.user_a = %s),
                #         user_a (blocked_id = %s in fragment — requesting user),
                #         user_a (blocker_id = %s in fragment — requesting user).
                cur.execute(sql, (pair_ab[0], str(user_a), str(user_a)))
                rows = cur.fetchall()

            # The blocked friendship must not appear
            self.assertEqual(
                rows, [],
                "Expected friendship row to be filtered out when user_a blocks user_b",
            )

            # user_c/user_d have no block — their row must appear
            with conn.cursor() as cur:
                cur.execute(sql, (pair_cd[0], str(user_c), str(user_c)))
                rows = cur.fetchall()
            self.assertEqual(len(rows), 1, "Unblocked friendship should be visible")

            # Reverse direction: user_b blocks user_a
            with conn.cursor() as cur:
                # Remove forward block, insert reverse
                cur.execute(
                    "DELETE FROM blocks WHERE blocker_id = %s AND blocked_id = %s",
                    (user_a, user_b),
                )
                cur.execute(
                    "INSERT INTO blocks (blocker_id, blocked_id) VALUES (%s, %s)",
                    (user_b, user_a),
                )

            with conn.cursor() as cur:
                cur.execute(sql, (pair_ab[0], str(user_a), str(user_a)))
                rows = cur.fetchall()

            self.assertEqual(
                rows, [],
                "Expected friendship row to be filtered out when user_b blocks user_a",
            )

        finally:
            with conn.cursor() as cur:
                # Clean up in dependency order
                cur.execute(
                    "DELETE FROM blocks WHERE blocker_id IN (%s,%s,%s,%s) OR blocked_id IN (%s,%s,%s,%s)",
                    (user_a, user_b, user_c, user_d, user_a, user_b, user_c, user_d),
                )
                cur.execute(
                    "DELETE FROM friendships WHERE user_a IN (%s,%s) OR user_b IN (%s,%s)",
                    (uuid.UUID(pair_ab[0]), uuid.UUID(pair_cd[0]),
                     uuid.UUID(pair_ab[1]), uuid.UUID(pair_cd[1])),
                )
                cur.execute(
                    "DELETE FROM users WHERE user_id IN (%s,%s,%s,%s)",
                    (user_a, user_b, user_c, user_d),
                )
            conn.close()


# ---------------------------------------------------------------------------
# is_blocked tests
# ---------------------------------------------------------------------------


class TestIsBlockedForwardBlock(unittest.TestCase):
    """
    Given a blocks row where user_a is blocker_id and user_b is blocked_id,
    when is_blocked(conn, user_a, user_b) is called,
    then it returns True.
    """

    def test_given_forward_block_when_is_blocked_then_returns_true(self):
        from knotify_obs import is_blocked

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchone.return_value = (1,)  # a block exists

        result = is_blocked(mock_conn, "user-a-uuid", "user-b-uuid")

        self.assertTrue(result)

    def test_given_reverse_block_when_is_blocked_then_returns_true(self):
        from knotify_obs import is_blocked

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchone.return_value = (1,)  # a block exists (reverse direction)

        result = is_blocked(mock_conn, "user-b-uuid", "user-a-uuid")

        self.assertTrue(result)

    def test_given_no_block_when_is_blocked_then_returns_false(self):
        from knotify_obs import is_blocked

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchone.return_value = None  # no block

        result = is_blocked(mock_conn, "user-a-uuid", "user-b-uuid")

        self.assertFalse(result)


class TestIsBlockedUsesParameterBinding(unittest.TestCase):
    """
    Given any two user UUIDs,
    when is_blocked is called,
    then it issues the SELECT via cur.execute(sql, (user_a, user_b, user_b, user_a))
    with parameter binding — no f-string interpolation of the user values into SQL.
    """

    def test_given_two_uuids_when_is_blocked_then_execute_called_with_four_param_tuple(self):
        from knotify_obs import is_blocked

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchone.return_value = None

        user_a = "aaaa-aaaa"
        user_b = "bbbb-bbbb"

        is_blocked(mock_conn, user_a, user_b)

        # execute must have been called exactly once
        mock_cur.execute.assert_called_once()
        call_args = mock_cur.execute.call_args
        # Second argument (the params tuple) must be (user_a, user_b, user_b, user_a)
        _, positional = call_args[0], call_args[0]
        sql_arg, params_arg = positional[0], positional[1]

        self.assertEqual(
            params_arg,
            (user_a, user_b, user_b, user_a),
            "is_blocked must call cur.execute(sql, (user_a, user_b, user_b, user_a))",
        )

        # The SQL itself must NOT contain the literal UUID values
        self.assertNotIn(user_a, sql_arg)
        self.assertNotIn(user_b, sql_arg)


class TestIsBlockedAgainstRealDb(unittest.TestCase):
    """
    Integration-style test: run is_blocked against a real docker-compose Postgres.
    Skips if the DB is not available.
    """

    def test_given_real_db_when_forward_block_then_is_blocked_returns_true(self):
        import os
        import uuid
        import psycopg2
        import psycopg2.extras

        psycopg2.extras.register_uuid()

        host = os.environ.get("PGHOST", "localhost")
        port = int(os.environ.get("PGPORT", "5432"))
        dbname = os.environ.get("PGDATABASE", "knotify")
        master_user = "knotify"
        master_pass = os.environ.get("PGPASSWORD", "knotify")

        try:
            conn = psycopg2.connect(
                host=host, port=port, dbname=dbname,
                user=master_user, password=master_pass,
            )
        except psycopg2.OperationalError:
            self.skipTest("docker-compose DB not available")

        conn.autocommit = True

        user_a = uuid.uuid4()
        user_b = uuid.uuid4()

        try:
            with conn.cursor() as cur:
                for uid, email in [
                    (user_a, f"isblk_a_{uuid.uuid4().hex[:6]}@test.invalid"),
                    (user_b, f"isblk_b_{uuid.uuid4().hex[:6]}@test.invalid"),
                ]:
                    cur.execute(
                        "INSERT INTO users (user_id, email) VALUES (%s, %s)", (uid, email)
                    )
                # Forward block: user_a → user_b
                cur.execute(
                    "INSERT INTO blocks (blocker_id, blocked_id) VALUES (%s, %s)",
                    (user_a, user_b),
                )

            from knotify_obs import is_blocked

            # Forward direction
            self.assertTrue(is_blocked(conn, str(user_a), str(user_b)))
            # Reverse direction also returns True (block is bidirectional by design)
            self.assertTrue(is_blocked(conn, str(user_b), str(user_a)))

        finally:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM blocks WHERE blocker_id IN (%s,%s) OR blocked_id IN (%s,%s)",
                    (user_a, user_b, user_a, user_b),
                )
                cur.execute(
                    "DELETE FROM users WHERE user_id IN (%s,%s)", (user_a, user_b)
                )
            conn.close()

    def test_given_real_db_when_no_block_then_is_blocked_returns_false(self):
        import os
        import uuid
        import psycopg2
        import psycopg2.extras

        psycopg2.extras.register_uuid()

        host = os.environ.get("PGHOST", "localhost")
        port = int(os.environ.get("PGPORT", "5432"))
        dbname = os.environ.get("PGDATABASE", "knotify")
        master_user = "knotify"
        master_pass = os.environ.get("PGPASSWORD", "knotify")

        try:
            conn = psycopg2.connect(
                host=host, port=port, dbname=dbname,
                user=master_user, password=master_pass,
            )
        except psycopg2.OperationalError:
            self.skipTest("docker-compose DB not available")

        conn.autocommit = True

        user_a = uuid.uuid4()
        user_b = uuid.uuid4()

        try:
            with conn.cursor() as cur:
                for uid, email in [
                    (user_a, f"noblk_a_{uuid.uuid4().hex[:6]}@test.invalid"),
                    (user_b, f"noblk_b_{uuid.uuid4().hex[:6]}@test.invalid"),
                ]:
                    cur.execute(
                        "INSERT INTO users (user_id, email) VALUES (%s, %s)", (uid, email)
                    )
            # No blocks inserted

            from knotify_obs import is_blocked

            self.assertFalse(is_blocked(conn, str(user_a), str(user_b)))
            self.assertFalse(is_blocked(conn, str(user_b), str(user_a)))

        finally:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM users WHERE user_id IN (%s,%s)", (user_a, user_b)
                )
            conn.close()


if __name__ == "__main__":
    unittest.main()
