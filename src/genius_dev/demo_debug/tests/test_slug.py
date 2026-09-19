import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from slug import slugify  # noqa: E402


class TestSlug(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_punctuation(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")

    def test_collapse(self):
        self.assertEqual(slugify("a   b"), "a-b")

    def test_strip(self):
        self.assertEqual(slugify("  Padded  "), "padded")


if __name__ == "__main__":
    unittest.main()
