"""Open a pd side for maintenance, and close it again (`pd open`, `pd close`).

Works from the RAM-only setup environment and from an ordinary live ISO
(Debian Live, Tails, ...) with cryptsetup, lvm2, thin-provisioning-tools,
btrfs-progs and argon2 installed.  An open side stays open after `pd open`
returns; the steps to undo are recorded in /run/pd/open.json (no keys) for
`pd close`.

Read-only opening (the default) never writes to the disk: every mapping is
read-only, thin LVs are mapped from thin_dump output instead of activating
the pool (dm-thin can't run in a read-only table), btrfs is mounted with
rescue=nologreplay and ext4 with noload.  --rw activates the VG and mounts
read-write.
"""

import json
import os
import pathlib
import re
import subprocess
import xml.etree.ElementTree as ET

from pd import layout as L
from pd import setup
from pd.config import CIPHER_ARGS, Config
from pd.util import argon2, cmd, cmd_output, mounted, mountpoint, plain_crypt

STATE = pathlib.Path("/run/pd/open.json")
RO_PREFIX = "pd-ro-"


class Session:
    """Undo steps, saved after each step so `pd close` works even after a
    failure part-way through opening."""

    def __init__(self, state: pathlib.Path = STATE):
        self.state = state
        self.undo: list[list[str]] = []

    def add(self, *step: str):
        self.undo.append(list(step))
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text(json.dumps({"undo": self.undo}))

    def close(self):
        errors = []
        for step in reversed(self.undo):
            kind, arg = step[0], step[1:]
            try:
                if kind == "umount":
                    cmd("umount", arg[0])
                elif kind == "rmdir":
                    os.rmdir(arg[0])
                elif kind == "vgchange-an":
                    setup.lvm("vgchange", "-an", "--devices", arg[1], arg[0])
                elif kind == "dmremove":
                    cmd("dmsetup", "remove", "--retry", arg[0])
                elif kind == "cryptclose":
                    cmd("cryptsetup", "close", arg[0])
            except (subprocess.CalledProcessError, OSError) as e:
                errors.append(f"{step}: {e}")
        # Without udev, removed devices leave stale /dev/mapper nodes.
        cmd("dmsetup", "mknodes", check=False, stderr=subprocess.DEVNULL)
        self.state.unlink(missing_ok=True)
        if errors:
            raise RuntimeError("some steps failed:\n" + "\n".join(errors))

    @staticmethod
    def load(state: pathlib.Path = STATE) -> "Session":
        s = Session(state)
        s.undo = json.loads(state.read_text())["undo"]
        return s


def read_boot_volume(cfg: Config, offset: int, password: bytes):
    """data.key, table and the systems' env.sh settings of a side."""
    key = argon2(password, cfg.salt, cfg.argon2)
    try:
        with setup.boot_volume(cfg.disk, offset, key, create=False) as mnt:
            data_key = (mnt / "data.key").read_bytes()
            table = (mnt / "table.txt").read_text()
            systems = {}
            for d in sorted((mnt / "systems").iterdir()):
                if d.is_dir() and not d.is_symlink() and (d / "env.sh").exists():
                    env = dict(re.findall(r"^(\w+)='?(.*?)'?$", (d / "env.sh").read_text(), re.M))
                    systems[d.name] = env
    except subprocess.CalledProcessError:
        raise RuntimeError("that password doesn't open a boot volume at that offset")
    return data_key, table, systems


def lvm_ro(pv: str, *args: str) -> str:
    return cmd_output("lvm", *args, "--readonly", "--devices", pv,
                      "--config", setup.LVM_NO_ARCHIVE, stderr=subprocess.DEVNULL)


def lv_linear_table(vg: str, lv: str, pv: str, pe_start: int) -> str:
    out = lvm_ro(pv, "lvs", "-a", "--noheadings", "--nosuffix", "--units", "s",
                 "-o", "seg_start,seg_size,seg_pe_ranges,vg_extent_size", f"{vg}/{lv}")
    lines = []
    for row in out.splitlines():
        start, size, ranges, extent = row.split()
        first = int(ranges.rsplit(":", 1)[1].split("-")[0])
        lines.append(f"{start} {size} linear {pv} {pe_start + first * int(extent)}")
    return "\n".join(lines) + "\n"


def thin_table(xml_text: str, tdata: str, size: int) -> str:
    """A read-only dm table equivalent to one thin device, from thin_dump XML."""
    root = ET.fromstring(xml_text)
    bs = int(root.get("data_block_size"))
    lines, pos = [], 0

    def emit(vstart, length, dstart):
        nonlocal pos
        if vstart > pos:
            lines.append(f"{pos} {vstart - pos} zero")
        lines.append(f"{vstart} {length} linear {tdata} {dstart}")
        pos = vstart + length

    for m in root.iter():
        if m.tag == "range_mapping":
            emit(int(m.get("origin_begin")) * bs, int(m.get("length")) * bs,
                 int(m.get("data_begin")) * bs)
        elif m.tag == "single_mapping":
            emit(int(m.get("origin_block")) * bs, bs, int(m.get("data_block")) * bs)
    if pos < size:
        lines.append(f"{pos} {size - pos} zero")
    return "\n".join(lines) + "\n"


def dm_create(session: Session, name: str, table: str, readonly: bool):
    cmd("dmsetup", "create", name, *(["--readonly"] if readonly else []), input=table.encode())
    session.add("dmremove", name)
    cmd("dmsetup", "mknodes", name, check=False)


def thin_ro_devices(session: Session, vg: str, pv: str, lvs: list[str]) -> dict[str, str]:
    """Read-only device paths for thin LVs, without activating their pool."""
    pe_start = int(lvm_ro(pv, "pvs", "--noheadings", "--nosuffix", "--units", "s",
                          "-o", "pe_start", pv))
    out = {}
    pools_mapped = {}
    for lv in lvs:
        pool, thin_id, size = lvm_ro(pv, "lvs", "--noheadings", "--nosuffix", "--units", "s",
                                     "-o", "pool_lv,thin_id,lv_size", f"{vg}/{lv}").split()
        if pool not in pools_mapped:
            for part in ("tmeta", "tdata"):
                dm_create(session, f"{RO_PREFIX}{pool}-{part}",
                          lv_linear_table(vg, f"{pool}_{part}", pv, pe_start), readonly=True)
            pools_mapped[pool] = True
        xml_text = cmd_output("thin_dump", "--dev-id", thin_id, f"/dev/mapper/{RO_PREFIX}{pool}-tmeta")
        dm_create(session, f"{RO_PREFIX}{lv}",
                  thin_table(xml_text, f"/dev/mapper/{RO_PREFIX}{pool}-tdata", int(size)),
                  readonly=True)
        out[lv] = f"/dev/mapper/{RO_PREFIX}{lv}"
    return out


def fstype(dev: str) -> str:
    return cmd_output("blkid", "-o", "value", "-s", "TYPE", dev)


def makedirs(session: Session, path: pathlib.Path):
    """mkdir -p, recording each directory created so close removes it."""
    missing = []
    while not path.exists():
        missing.append(path)
        path = path.parent
    for d in reversed(missing):
        d.mkdir()
        session.add("rmdir", str(d))


def mount(session: Session, dev: str, target: pathlib.Path, fs: str, opts: str):
    try:
        makedirs(session, target)
    except OSError:
        # Inside a read-only mount: the OS never created this directory.
        print(f"pd: skipping {target} (no such directory in a read-only mount)")
        return
    cmd("mount", "-t", fs, "-o", opts, dev, target)
    session.add("umount", str(target))


def open_side(cfg: Config, password: bytes, offset: int, *, rw: bool,
              mnt: pathlib.Path, state: pathlib.Path = STATE) -> Session:
    if state.exists():
        raise RuntimeError(f"a side is already open ({state}); run `pd close` first")
    data_key, table, systems = read_boot_volume(cfg, offset, password)
    vg = next(iter(systems.values()))["VG"] if systems else cfg.vg
    session = Session(state)
    try:
        dm_create(session, setup.RAW_MAP, table.replace("DISK", cfg.disk), readonly=not rw)
        r, w = os.pipe()
        os.write(w, data_key)
        os.close(w)
        try:
            cmd("cryptsetup", "open", "--type=plain", "--batch-mode", *CIPHER_ARGS,
                *([] if rw else ["--readonly"]), f"--key-file=/proc/self/fd/{r}",
                f"/dev/mapper/{setup.RAW_MAP}", setup.DATA_MAP, pass_fds=(r,))
        finally:
            os.close(r)
        session.add("cryptclose", setup.DATA_MAP)
        pv = f"/dev/mapper/{setup.DATA_MAP}"

        root_lvs = {name: env["LV_ROOT"] for name, env in systems.items()}
        others = [lv for lv in lvm_ro(pv, "lvs", "--noheadings", "-o", "lv_name,segtype", vg).splitlines()]
        volumes = [l.split()[0] for l in others
                   if l.split()[1] == "thin" and l.split()[0] not in root_lvs.values()]
        if rw:
            setup.lvm("vgchange", "-ay", "--ignoremonitoring", "--devices", pv, vg)
            session.add("vgchange-an", vg, pv)
            devs = {lv: f"/dev/{vg}/{lv}" for lv in list(root_lvs.values()) + volumes}
        else:
            devs = thin_ro_devices(session, vg, pv, list(root_lvs.values()) + volumes)

        makedirs(session, mnt)
        for name, lv in root_lvs.items():
            for subvol, path in cfg.subvolumes.items():
                opts = (setup.ROOT_OPTS if rw else "ro,nodiscard,rescue=nologreplay") + f",subvol={subvol}"
                mount(session, devs[lv], mnt / name / path, "btrfs", opts)
        for lv in volumes:
            fs = fstype(devs[lv])
            if rw:
                opts = setup.VOLUME_OPTS.get(fs, "noatime,nodiscard")
            else:
                opts = {"btrfs": "ro,nodiscard,rescue=nologreplay", "ext4": "ro,noload"}.get(fs, "ro")
            mount(session, devs[lv], mnt / "volumes" / lv, fs, opts)
    except BaseException:
        session.close()
        raise
    return session


def close(state: pathlib.Path = STATE):
    if not state.exists():
        raise RuntimeError("nothing is open")
    Session.load(state).close()
