"""The disk's edges and the salt (docs/design.md §3, §4).

The first and last MiB of a pd disk hold random data and nothing else.  The
salt for both sides' Argon2 is the 32 bytes just below 1 MiB, read back from
the disk rather than chosen, so the boot image carries nothing specific to
an install.  An edge that happens to look like a partition table or a
filesystem is refilled: the boot tries such disks last, and tools treat them
as in use.  The checks are pd/disk_class.sh, shared with the boot, plus
libblkid's full set of probes (wipefs) here.

A config with an explicit `salt` (stock boot, or an install from before the
salt moved to the disk) uses that instead, and the edges don't matter.
"""

import dataclasses
import importlib.resources
import os
import pathlib
import subprocess

from pd import layout as L
from pd.config import Config
from pd.util import cmd, cmd_output

SCRIPT = importlib.resources.files("pd").joinpath("disk_class.sh").read_text()


def _sh(function: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", "-c", f'{SCRIPT}\n{function} "$@"', "sh", *args],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)


def disk_class(dev: str) -> str:
    """small, blank, used or plain (pd/disk_class.sh)."""
    return _sh("disk_class", dev).stdout.strip()


def salt_blank(salt: str) -> bool:
    return _sh("salt_blank", salt).returncode == 0


def disk_salt(disk: str) -> str:
    """The salt stored on the disk, as hex (it may be blank)."""
    with open(disk, "rb") as f:
        f.seek(L.SALT_OFFSET)
        return f.read(L.SALT_LEN).hex()


def salt(cfg: Config) -> str:
    """The salt both sides' Argon2 uses: the config's if it sets one,
    otherwise the disk's."""
    if cfg.salt:
        return cfg.salt
    s = disk_salt(cfg.disk)
    if salt_blank(s):
        raise RuntimeError(f"{cfg.disk} has no salt in its first MiB: run `pd randomize` on a new disk, "
                           "or set `salt` in the config (a stock decoy, or an install from before the "
                           "salt moved to the disk: see docs/migration.md)")
    return s


def signatures(dev: str) -> list[str]:
    """Everything libblkid finds on dev (partition tables and superblocks,
    including those at the end of the device)."""
    out = cmd_output("wipefs", "--no-act", "--noheadings", "--output", "TYPE,OFFSET", dev)
    return [line.strip() for line in out.splitlines() if line.strip()]


def edge_problems(disk: str) -> list[str]:
    """Why the disk's edges don't look like random data (empty if they do)."""
    problems = []
    cls = disk_class(disk)
    if cls != "plain":
        problems.append(f"the boot would treat it as {cls}")
    problems += [f"signature {s}" for s in signatures(disk)]
    return problems


def _write(disk: str, offset: int, data: bytes) -> None:
    fd = os.open(disk, os.O_WRONLY)
    try:
        if os.pwrite(fd, data, offset) != len(data):
            raise RuntimeError(f"short write to {disk} at {offset}")
        os.fsync(fd)
    finally:
        os.close(fd)


def write_edges(disk: str, *, header: bool = True, trailer: bool = True, tries: int = 20) -> None:
    """Fill the disk's first and/or last MiB with fresh random data, again
    until nothing recognizes them (about once in 65,536 disks, the MBR boot
    signature turns up)."""
    end = L.disk_size(int(cmd_output("blockdev", "--getsize64", disk)))
    for _ in range(tries):
        if header:
            _write(disk, 0, os.urandom(L.HEADER))
        if trailer:
            _write(disk, end - L.TRAILER, os.urandom(L.TRAILER))
        reread(disk)
        if not edge_problems(disk):
            return
    raise RuntimeError(f"couldn't make {disk}'s edges look random: {'; '.join(edge_problems(disk))}")


def check_header_clear(cfg: Config, table: str, data_key: bytes) -> None:
    """Refuse if a hidden data region (v1's covers the whole disk from 0) has
    data in the 256 MiB chunk holding the header.  It allocates that chunk
    last, after all the decoy's space, so normally it's untouched."""
    lo = L.table_physical_range(table)[0]
    if lo >= L.HEADER:
        return
    from pd import compact
    if compact.region_low(cfg, table, data_key) <= lo:
        raise RuntimeError("the hidden side has data at the start of the disk, where the header goes")


def fix_edges(disk: str) -> None:
    """After a random fill: refill the edges only if something recognizes
    them."""
    reread(disk)
    if edge_problems(disk):
        write_edges(disk)


def reread(disk: str) -> None:
    """Have the kernel re-read the partition table (fails harmlessly while
    something on the disk is open)."""
    cmd("blockdev", "--rereadpt", disk, check=False, stderr=subprocess.DEVNULL)


def ensure(cfg: Config, *, keep: bool) -> None:
    """Before creating a side on a disk that uses the disk salt: give it
    random edges unless it has them.  `keep`: other data depends on the
    current salt (re-creating the decoy next to a hidden side), so refuse
    instead of replacing it."""
    if cfg.salt:
        return
    if keep:
        if disk_class(cfg.disk) in ("small", "blank"):
            raise RuntimeError(f"{cfg.disk}'s first MiB doesn't hold a salt, but the hidden side "
                               "depends on it")
        return
    if edge_problems(cfg.disk):
        write_edges(cfg.disk)


# --- Changing the header (`pd header`) --------------------------------------
#
# Both sides' boot volumes are keyed under the salt, so a new header means
# re-keying both.  Order, so that an interruption can be finished by running
# the command again:
#   1. save the old salt next to the config (it isn't secret);
#   2. write the new header (and the trailer, where the layout leaves it unused);
#   3. re-key each side that still opens with the old salt;
#   4. check both open with the new one, and delete the saved salt.

@dataclasses.dataclass
class Side:
    offset: int          # bytes
    password: bytes


def pending_path(config_path: str) -> pathlib.Path:
    p = pathlib.Path(config_path)
    return p.with_name(p.name + ".old-salt")


def change(cfg: Config, config_path: str, sides: list[Side], *, old_salt: str | None = None,
           next_key=None, report=print) -> str:
    """Give the disk a new random header and re-key `sides` (the decoy
    first, then the hidden side if any) from the old salt to the new one.
    The old salt: `old_salt`, else the config's, else the disk's current one.
    Returns the new salt."""
    from pd import rekey, setup
    pending = pending_path(config_path)
    resuming = pending.exists()
    if resuming:
        old = pending.read_text().strip()
        report(f"finishing an interrupted header change (old salt from {pending})")
    else:
        old = old_salt or cfg.salt
        if not old:
            cls = disk_class(cfg.disk)
            if cls != "plain":
                raise RuntimeError(f"{cfg.disk}'s first MiB doesn't hold a salt (it looks {cls}); "
                                   "pass the current salt with --old-salt")
            old = disk_salt(cfg.disk)
        else:
            # Moving the salt to the disk: nothing may live in the first MiB
            # yet (it's zeros on a pd disk; a stock decoy has a partition
            # table there).
            with open(cfg.disk, "rb") as f:
                if f.read(L.HEADER) != bytes(L.HEADER):
                    raise RuntimeError(f"{cfg.disk}'s first MiB isn't all zeros; refusing to overwrite it")
    old_cfg = dataclasses.replace(cfg, salt=old)
    new_cfg = dataclasses.replace(cfg, salt="")
    if not resuming or disk_salt(cfg.disk) == old or disk_class(cfg.disk) != "plain":
        # Every side must open, and the layout allow it, before anything is
        # written.
        d = L.disk_size(int(cmd_output("blockdev", "--getsize64", cfg.disk)))
        trailer = True
        for side in sides:
            found = setup.find_boot_key(old_cfg, side.offset, side.password)
            if found is None:
                raise RuntimeError(f"the password for the side at sector {side.offset // L.SECTOR} "
                                   "doesn't open it with the current salt")
            with setup.boot_volume(cfg.disk, side.offset, found[0], create=False) as mnt:
                table, data_key = (mnt / "table.txt").read_text(), (mnt / "data.key").read_bytes()
            if side.offset == L.DECOY_PLAIN_OFFSET:
                # On a layout migrated from v1, the decoy's data region
                # reaches the end of the disk: leave the trailer alone.
                trailer = L.table_physical_range(table)[1] <= L.data_end(d)
            else:
                check_header_clear(cfg, table, data_key)
        if not resuming:
            pending.write_text(old + "\n")
        report("writing a new random header")
        write_edges(cfg.disk, trailer=trailer)
    new = salt(new_cfg)
    for side in sides:
        if setup.find_boot_key(new_cfg, side.offset, side.password) is not None:
            continue
        report(f"re-keying the side at sector {side.offset // L.SECTOR}")
        for w in rekey.rekey(old_cfg, side.offset, side.password, side.password, new_salt=new,
                             **({"next_key": next_key} if next_key else {})):
            report(f"warning: {w}")
    for side in sides:
        if setup.find_boot_key(new_cfg, side.offset, side.password) is None:
            raise RuntimeError(f"the side at sector {side.offset // L.SECTOR} doesn't open with the new "
                               f"salt; the old one is in {pending}")
    pending.unlink()
    return new
