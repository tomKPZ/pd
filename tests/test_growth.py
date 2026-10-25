"""On a disk set up without randomizing, the decoy's writes must all be at
the front and the hidden side's at the back, with untouched space between.
This is the core trick (design.md §2), checked on the real setup."""

import unittest

from pd import layout as L
from tests import fixture, visualize, vm


class Growth(unittest.TestCase):
    def test_two_sides_grow_from_opposite_ends(self):
        fx = fixture.make(randomize=False)
        self.addCleanup(vm.cleanup, fx.work)
        m = visualize.chunk_map(str(fx.disk))
        print("\n" + visualize.render(m))
        runs = visualize.runs(m)
        mib = L.MIB >> 20
        # Expected: zero header, then everything the decoy wrote, one large
        # untouched gap, then everything the hidden side wrote (its data,
        # its boot volume) and the decoy's end-of-disk metadata.
        self.assertFalse(runs[0][0], "first MiB must be zero")
        self.assertEqual(runs[0][2], 0)
        gaps = [r for r in runs if not r[0]]
        biggest = max(gaps, key=lambda r: r[2] - r[1])
        size_mib = len(m)
        self.assertGreater(biggest[2] - biggest[1], size_mib // 2,
                           "expected one large untouched gap in the middle")
        # Small holes are fine (unwritten parts of allocated chunks), but
        # nothing may be written inside the big gap.
        front = [r for r in runs if r[0] and r[2] < biggest[1]]
        back = [r for r in runs if r[0] and r[1] > biggest[2]]
        self.assertTrue(front and back)
        self.assertGreater(back[0][1], size_mib // 2)


if __name__ == "__main__":
    unittest.main()
