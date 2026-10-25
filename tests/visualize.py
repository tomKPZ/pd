"""Disk visualizer (revived from the 2024 prototype): show which 1 MiB
chunks of a disk image are non-zero.  On a disk set up *without*
randomizing, this shows the decoy growing from the front and the hidden side
from the back.

Usage: python3 -m tests.visualize IMAGE [CHUNK_MIB]
"""

import sys

ZERO_CHECK = 1 << 20


def chunk_map(path: str, chunk: int = 1 << 20) -> list[bool]:
    """True for each chunk that contains any non-zero byte."""
    zero = bytes(chunk)
    out = []
    with open(path, "rb") as f:
        while True:
            data = f.read(chunk)
            if not data:
                break
            out.append(data != zero[: len(data)])
    return out


def runs(m: list[bool]) -> list[tuple[bool, int, int]]:
    """(used, first, last) runs of equal values."""
    out = []
    start = 0
    for i in range(1, len(m) + 1):
        if i == len(m) or m[i] != m[start]:
            out.append((m[start], start, i - 1))
            start = i
    return out


def render(m: list[bool], width: int = 128) -> str:
    """One character per bucket: '#' if any chunk in it is used."""
    per = max(1, len(m) // width)
    return "".join("#" if any(m[i:i + per]) else "." for i in range(0, len(m), per))


if __name__ == "__main__":
    chunk = int(sys.argv[2]) << 20 if len(sys.argv) > 2 else 1 << 20
    m = chunk_map(sys.argv[1], chunk)
    print(render(m))
    for used, a, b in runs(m):
        print(f"{'used' if used else 'zero'} {a}-{b}")

