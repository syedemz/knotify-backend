"""
Unit tests for chat_room_id helper.

Story 6.0b AC:
  - chat_room_id(a, b) is symmetric: chat_room_id(a, b) == chat_room_id(b, a)
  - deterministic across calls with the same inputs
  - returns a 64-character lowercase hex string (SHA-256 hex digest)
  - the digest is based on the lexicographically ordered pair "<min>:<max>" so
    the smaller UUID always comes first regardless of call order
"""

import unittest


class TestChatRoomIdSymmetry(unittest.TestCase):
    """
    Given two user UUIDs a and b,
    when chat_room_id is called in both orders,
    then both calls return the same room ID.
    """

    def test_given_two_uuids_when_called_both_orders_then_results_are_equal(self):
        from knotify_obs import chat_room_id

        a = "11111111-1111-1111-1111-111111111111"
        b = "22222222-2222-2222-2222-222222222222"

        self.assertEqual(chat_room_id(a, b), chat_room_id(b, a))

    def test_given_lexicographically_reversed_uuids_when_called_both_orders_then_equal(self):
        from knotify_obs import chat_room_id

        # b is lexicographically smaller than a
        a = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        b = "00000000-0000-0000-0000-000000000000"

        self.assertEqual(chat_room_id(a, b), chat_room_id(b, a))


class TestChatRoomIdDeterminism(unittest.TestCase):
    """
    Given the same pair of user UUIDs,
    when chat_room_id is called multiple times,
    then it returns the identical string each time.
    """

    def test_given_same_inputs_when_called_twice_then_same_result(self):
        from knotify_obs import chat_room_id

        a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

        first = chat_room_id(a, b)
        second = chat_room_id(a, b)

        self.assertEqual(first, second)


class TestChatRoomIdFormat(unittest.TestCase):
    """
    Given any two user UUIDs,
    when chat_room_id is called,
    then the result is a 64-character lowercase hex string.
    """

    def test_given_valid_uuids_when_chat_room_id_then_returns_64_char_hex(self):
        from knotify_obs import chat_room_id

        a = "11111111-1111-1111-1111-111111111111"
        b = "22222222-2222-2222-2222-222222222222"

        result = chat_room_id(a, b)

        self.assertEqual(len(result), 64)
        # Must be valid lowercase hex
        int(result, 16)  # raises ValueError if not hex
        self.assertEqual(result, result.lower())

    def test_given_same_uuid_for_both_args_when_chat_room_id_then_returns_64_char_hex(self):
        from knotify_obs import chat_room_id

        same = "cccccccc-cccc-cccc-cccc-cccccccccccc"

        result = chat_room_id(same, same)

        self.assertEqual(len(result), 64)
        int(result, 16)
        self.assertEqual(result, result.lower())


class TestChatRoomIdCorrectHash(unittest.TestCase):
    """
    Given user UUIDs a and b where a < b lexicographically,
    when chat_room_id is called,
    then the result equals sha256("<a>:<b>".encode()).hexdigest().
    """

    def test_given_known_pair_when_chat_room_id_then_matches_expected_sha256(self):
        import hashlib
        from knotify_obs import chat_room_id

        a = "11111111-1111-1111-1111-111111111111"
        b = "22222222-2222-2222-2222-222222222222"

        # a < b lexicographically, so canonical form is "a:b"
        expected = hashlib.sha256(f"{a}:{b}".encode("utf-8")).hexdigest()

        self.assertEqual(chat_room_id(a, b), expected)
        self.assertEqual(chat_room_id(b, a), expected)

    def test_given_pair_where_second_is_smaller_when_chat_room_id_then_uses_lex_min_first(self):
        import hashlib
        from knotify_obs import chat_room_id

        # b is lex-smaller than a
        a = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        b = "00000000-0000-0000-0000-000000000000"

        # canonical: min=b, max=a
        expected = hashlib.sha256(f"{b}:{a}".encode("utf-8")).hexdigest()

        self.assertEqual(chat_room_id(a, b), expected)
        self.assertEqual(chat_room_id(b, a), expected)


if __name__ == "__main__":
    unittest.main()
