from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DockerfileTests(unittest.TestCase):
    def test_runtime_image_copies_v02_app_package(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("COPY app/ /app/app/", dockerfile)


if __name__ == "__main__":
    unittest.main()
