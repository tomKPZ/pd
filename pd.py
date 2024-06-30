#!/usr/bin/env python3

import base64
import contextlib
import os
import subprocess
import sys
import tempfile
import uuid

DISK_SIZE = "1G"
PASSWORD = "test"


def cmd(*args, check=True, **kwargs):
    return subprocess.run(*args, check=check, **kwargs)


def cmd_output(*args, **kwargs):
    return cmd(*args, stdout=subprocess.PIPE, **kwargs).stdout.decode().strip()


def dd(**kwargs):
    OPS = {
        "bs",
        "cbs",
        "conv",
        "count",
        "ibs",
        "if",
        "iflag",
        "obs",
        "of",
        "oflag",
        "seek",
        "skip",
        "status",
    }
    dd_cmd = ["dd"]
    forward_kwargs = {}
    for k, v in kwargs.items():
        k = k.lstrip("_")
        if k in OPS:
            dd_cmd.append(f"{k}={v}")
        else:
            forward_kwargs[k] = v
    return cmd(dd_cmd, **forward_kwargs)


@contextlib.contextmanager
def disk_image():

    disk_img = tempfile.mktemp(suffix=".img")
    try:
        dd(_if="/dev/zero", of=disk_img, bs=DISK_SIZE, count=1, status="progress")
        yield disk_img
    finally:
        os.remove(disk_img)


@contextlib.contextmanager
def loopback_device(file_path):
    loop_dev = cmd_output(["losetup", "-f", "--show", file_path])
    try:
        yield loop_dev
    finally:
        cmd(["losetup", "-d", loop_dev])


@contextlib.contextmanager
def temp_file():
    file = tempfile.mktemp()
    try:
        yield file
    finally:
        os.remove(file)


@contextlib.contextmanager
def plain_dmcrypt(wrap_dev, key_file):
    crypt_name = f"dmcrypt_test_{uuid.uuid4()}"
    crypt_dev = f"/dev/mapper/{crypt_name}"
    cryptsetup_cmd = [
        "cryptsetup",
        "open",
        "--type",
        "plain",
        "--cipher",
        "aes-xts-plain64",
        "--key-size",
        "512",
        "--key-file",
        key_file,
        "--sector-size",
        "4096",
        wrap_dev,
        crypt_name,
    ]
    cmd(cryptsetup_cmd)
    try:
        yield crypt_dev
    finally:
        cmd(["cryptsetup", "close", crypt_name])


def setup_device(device):
    with temp_file() as key_file:
        dd(_if="/dev/urandom", of=key_file, bs=64, count=1)
        with open(key_file, mode="rb") as key:
            shred_key = base64.encodebytes(key.read()).decode()
            print("Using shredding key", shred_key.replace("\n", ""))
        with plain_dmcrypt(device, key_file) as plain:
            dd(_if="/dev/zero", of=plain, bs="4M", status="progress", check=False)
    dd(_if="/dev/zero", of=device, bs="4K", count=1)
    salt4k = dd(_if=device, bs="4K", skip=1, count=1, stdout=subprocess.PIPE).stdout
    salt512 = cmd_output(["sha512sum"], input=salt4k).split()[0]
    argon_cmd = [
        "argon2",
        salt512,
        "-id",
        "-t",
        "3",
        "-m",
        "12",
        "-p",
        "1",
        "-l",
        "128",
        "-r",
        "-v",
        "13",
    ]
    key_hex = (
        cmd(argon_cmd, input=PASSWORD.encode(), stdout=subprocess.PIPE)
        .stdout.decode()
        .strip()
    )
    key_bytes = bytes.fromhex(key_hex)
    assert len(key_bytes) == 128
    with temp_file() as key_file:
        dd(of=key_file, input=key_bytes)
        print(key_file)
        cmd(["hexdump", "-C", key_file])


def main() -> int:
    with disk_image() as disk_img:
        with loopback_device(disk_img) as loop:
            setup_device(loop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
