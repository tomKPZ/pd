import pathlib
import tempfile
import unittest

from pd import usb
from pd.config import Config


class UsbTest(unittest.TestCase):
    def test_salt_round_trip(self):
        cfg = Config(disk="/dev/null", salt="ab" * 32)
        blob = b"\x28\xb5\x2f\xfd" + bytes(1001) + usb.install_cpio(cfg) + bytes(77)
        with tempfile.NamedTemporaryFile() as f:
            f.write(blob)
            f.flush()
            self.assertEqual(usb.read_salt(pathlib.Path(f.name)), cfg.salt)

    def test_early_env_has_one_2048(self):
        env = usb.early_env(Config(disk="/dev/null", salt="ab" * 32))
        self.assertEqual(env.count("2048"), 1)
        self.assertIn("PLAIN_OFFSET=2048\n", env)


if __name__ == "__main__":
    unittest.main()
