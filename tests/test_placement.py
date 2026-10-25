import unittest

from pd import layout as L
from pd import setup

EXT = 4 << 20
PE_START = 1 << 20


def seg(lv, first, last):
    return {"lv": lv, "first": first, "last": last, "extent": EXT}


# What stock LVM produced for the decoy on an 8 GiB disk.
STOCK = [seg("swap", 0, 15), seg("lvol0_pmspare", 16, 17), seg("thinpool_tdata", 18, 1980),
         seg("thinpool_tmeta", 1981, 1982)]
# What the one-extent-then-extend trick produces (v1 and the hidden side).
FRONT = [seg("swap", 0, 15), seg("thinpool_tdata", 16, 16), seg("thinpool_tmeta", 17, 18),
         seg("lvol0_pmspare", 19, 20), seg("thinpool_tdata", 21, 1982)]
D = 8 << 30


def phys(extent):
    return L.DATA_START + PE_START + extent * EXT


class DecoyTopTest(unittest.TestCase):
    def test_stock_layout_top_is_tmeta(self):
        self.assertEqual(setup.decoy_top(STOCK, PE_START, L.DATA_START, D), phys(1981))

    def test_front_metadata_top_is_end_of_disk(self):
        self.assertEqual(setup.decoy_top(FRONT, PE_START, L.DATA_START, D), D - L.ALIGN)

    def test_hidden_fits_below_stock_tmeta(self):
        top = setup.decoy_top(STOCK, PE_START, L.DATA_START, D)
        hl = L.hidden_layout(top)
        setup.check_no_overlap(STOCK, PE_START, hl)
        self.assertLessEqual(hl.plain_offset + L.PLAIN_SIZE, phys(1981))

    def test_front_layout_passes(self):
        hl = L.hidden_layout(setup.decoy_top(FRONT, PE_START, L.DATA_START, D))
        setup.check_no_overlap(FRONT, PE_START, hl)

    def test_overlap_detected(self):
        # A decoy LV in the middle of the disk (e.g. a later lvcreate) must
        # stop hidden setup.
        segs = STOCK[:2] + [seg("thinpool_tdata", 18, 900), seg("extra", 901, 1000),
                            seg("thinpool_tdata", 1001, 1980), STOCK[3]]
        hl = L.hidden_layout(setup.decoy_top(STOCK, PE_START, L.DATA_START, D))
        with self.assertRaises(RuntimeError):
            setup.check_no_overlap(segs, PE_START, hl)


if __name__ == "__main__":
    unittest.main()
