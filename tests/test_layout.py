import unittest

from pd import layout as L


class LayoutTest(unittest.TestCase):
    def test_linear_table(self):
        self.assertEqual(
            L.linear_table(L.DATA_START, 4 * L.GIB),
            f"0 {(4 * L.GIB - L.DATA_START) // 512} linear DISK 524288\n",
        )

    def test_reversed_table_covers_region_backwards(self):
        start, end = L.DATA_START, L.DATA_START + 3 * L.REVERSE_CHUNK + 5 * L.MIB
        t = L.reversed_table(start, end)
        segs = L.parse_table(t)
        self.assertEqual(len(segs), 3)
        # Logical sectors are contiguous from 0.
        self.assertEqual([s[0] for s in segs], [0, 524288, 2 * 524288])
        # Logical 0 is the physically highest chunk, ending exactly at `end`.
        self.assertEqual(L.logical_to_physical(t, 0), end - L.REVERSE_CHUNK)
        lo, hi = L.table_physical_range(t)
        self.assertEqual(hi, end)
        self.assertGreaterEqual(lo, start)

    def test_reversed_table_nonzero_start(self):
        # v1's reversed_table() ignored `start`; make sure physical offsets
        # never go below it.
        t = L.reversed_table(L.DATA_START, L.DATA_START + 2 * L.REVERSE_CHUNK)
        lo, _ = L.table_physical_range(t)
        self.assertEqual(lo, L.DATA_START)

    def test_hidden_layout(self):
        d = 1000 * L.GIB
        top = d - 300 * L.MIB
        h = L.hidden_layout(top)
        self.assertLessEqual(h.plain_offset + L.PLAIN_SIZE, top)
        self.assertEqual(h.plain_offset % L.MIB, 0)
        lo, hi = L.table_physical_range(h.data_table())
        self.assertGreaterEqual(lo, L.DATA_START)
        self.assertLessEqual(hi, h.plain_offset)

    def test_memorable_offset(self):
        h = L.hidden_layout(1000 * L.GIB, memorable=True)
        self.assertEqual(h.plain_offset_sectors % 10**6, 0)
        self.assertLessEqual(h.plain_offset + L.PLAIN_SIZE, 1000 * L.GIB)

    def test_hidden_boot_command(self):
        h = L.hidden_layout(8 * L.GIB)
        self.assertEqual(
            L.hidden_boot_command(h), f"sed -i s/2048/{h.plain_offset_sectors}/ /early_env.sh"
        )


if __name__ == "__main__":
    unittest.main()

