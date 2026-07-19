import unittest

from bridge import TelegramClient


class TelegramClientTests(unittest.TestCase):
    def test_remote_end_closed_is_a_transient_get_updates_error(self):
        error = RuntimeError("Cannot reach https://api.telegram.org: Remote end closed connection without response")

        self.assertTrue(TelegramClient._is_transient_get_updates_error(error))


if __name__ == "__main__":
    unittest.main()
