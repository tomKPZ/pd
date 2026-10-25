"""Helpers for running external commands.  Every command is traced."""

import contextlib
import os
import pathlib
import subprocess
import tempfile

CMD_TRACE: list[tuple[str, ...]] = []


def cmd(*args, check=True, input: bytes | None = None, **kwargs) -> subprocess.CompletedProcess:
    args = tuple(str(a) for a in args)
    CMD_TRACE.append(args)
    return subprocess.run(args, check=check, input=input, **kwargs)


def cmd_output(*args, **kwargs) -> str:
    return cmd(*args, stdout=subprocess.PIPE, **kwargs).stdout.decode().strip()


def write_file(path: pathlib.Path, data: bytes | str, mode: int = 0o644) -> None:
    if isinstance(data, str):
        data = data.encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


@contextlib.contextmanager
def mountpoint(prefix: str = "pd-"):
    """A temporary directory under /run (tmpfs), removed afterwards."""
    base = pathlib.Path("/run") if os.access("/run", os.W_OK) else None
    path = pathlib.Path(tempfile.mkdtemp(prefix=prefix, dir=base))
    try:
        yield path
    finally:
        # Remove the (now empty) mount point directories, deepest first.
        # rmdir refuses non-empty directories, so nothing else can be lost.
        for d in sorted((p for p in path.rglob("*") if p.is_dir() and not p.is_symlink()),
                        key=lambda p: len(p.parts), reverse=True):
            d.rmdir()
        path.rmdir()


@contextlib.contextmanager
def mounted(device, target: pathlib.Path, fstype: str, options: str):
    cmd("mount", "-t", fstype, "-o", options, device, target)
    try:
        yield target
    finally:
        cmd("umount", target)


@contextlib.contextmanager
def dm_device(name: str, table: str, readonly: bool = False):
    cmd("dmsetup", "create", name, *(["--readonly"] if readonly else []), input=table.encode())
    # Without udev, nothing else creates the /dev/mapper node.
    cmd("dmsetup", "mknodes", name)
    try:
        yield pathlib.Path("/dev/mapper") / name
    finally:
        cmd("dmsetup", "remove", "--retry", name)


@contextlib.contextmanager
def plain_crypt(name: str, device, key: bytes, crypt_args: list[str], *,
                offset: int | None = None, size: int | None = None, readonly: bool = False):
    """Open plain dm-crypt.  The key goes through a pipe, never a file."""
    extra = []
    if offset is not None:
        extra.append(f"--offset={offset}")
    if size is not None:
        extra.append(f"--size={size}")
    if readonly:
        extra.append("--readonly")
    # cryptsetup hashes a plain-mode key read from stdin ("-") like a
    # passphrase, but uses a key file's bytes directly.  Pass the key as a
    # file: the read end of a pipe.
    r, w = os.pipe()
    try:
        os.write(w, key)
        os.close(w)
        cmd("cryptsetup", "open", "--type=plain", "--batch-mode", *crypt_args, *extra,
            f"--key-file=/proc/self/fd/{r}", device, name, pass_fds=(r,))
    finally:
        os.close(r)
    try:
        yield pathlib.Path("/dev/mapper") / name
    finally:
        cmd("cryptsetup", "close", name)


def argon2(password: bytes, salt: str, params: "Argon2Params") -> bytes:
    out = cmd_output("argon2", salt, *params.args(), input=password)
    key = bytes.fromhex(out)
    assert len(key) == params.length
    return key


class Argon2Params:
    def __init__(self, t: int = 14, m_log2: int = 18, p: int = 8, length: int = 64):
        self.t, self.m_log2, self.p, self.length = t, m_log2, p, length

    def args(self) -> list[str]:
        return ["-id", "-t", str(self.t), "-m", str(self.m_log2), "-p", str(self.p),
                "-l", str(self.length), "-v", "13", "-r"]

    def shell(self) -> str:
        return " ".join(self.args())

