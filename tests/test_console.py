"""app/console.py: printing a player's name that the console cannot show must not raise."""
import io
import sys
import unittest
from unittest import mock

from app.console import tolerant_output

NAME = "\u540d\u524d\u2603"            # Chinese characters and a snowman: not representable in cp1252


def strict_cp1252():
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")


class TolerantOutputTests(unittest.TestCase):
    def test_a_strict_stream_raises_and_a_tolerant_one_prints_question_marks(self):
        with self.assertRaises(UnicodeEncodeError):
            print(NAME, file=strict_cp1252(), flush=True)               # the problem this module solves
        out = strict_cp1252()
        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", strict_cp1252()):
            tolerant_output()
            print("hello " + NAME, flush=True)
        self.assertEqual(out.buffer.getvalue().decode("cp1252").strip(), "hello ???")

    def test_stand_in_streams_without_reconfigure_are_left_alone(self):
        with mock.patch.object(sys, "stdout", io.StringIO()), mock.patch.object(sys, "stderr", io.StringIO()):
            tolerant_output()                                           # must simply not raise


if __name__ == "__main__":
    unittest.main()
