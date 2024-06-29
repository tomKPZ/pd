#!/usr/bin/env python3

import base64
import contextlib
import os
import subprocess
import sys
import tempfile
import uuid


def cmd(*args, check=True, **kwargs):
    return subprocess.run(*args, check=check, **kwargs)


def cmd_output(*args, **kwargs):
    return cmd(*args, stdout=subprocess.PIPE, **kwargs).stdout.decode().strip()


@contextlib.contextmanager
def disk_image():
    DISK_SIZE = "1G"

    disk_img = tempfile.mktemp(suffix=".img")
    try:
        cmd(
            [
                "dd",
                "if=/dev/zero",
                f"of={disk_img}",
                f"bs={DISK_SIZE}",
                "count=1",
                "status=progress",
            ]
        )
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
def temp_file(size):
    file = tempfile.mktemp()
    try:
        cmd(
            [
                "dd",
                "if=/dev/urandom",
                f"of={file}",
                f"bs={size}",
                "count=1",
                "status=progress",
            ]
        )
        yield file
    finally:
        os.remove(file)


@contextlib.contextmanager
def plain_dmcrypt(wrap_dev):
    CRYPT_NAME = f"dmcrypt_test_{uuid.uuid4()}"
    CRYPT_DEV = f"/dev/mapper/{CRYPT_NAME}"
    with temp_file(64) as key:
        with open(key, mode="rb") as file:
            shred_key = base64.encodebytes(file.read()).decode()
            print("Using shredding key", shred_key.replace("\n", ""))
        cmd(
            [
                "cryptsetup",
                "open",
                "--type",
                "plain",
                "--cipher",
                "aes-xts-plain64",
                "--key-size",
                "512",
                "--key-file",
                key,
                "--sector-size",
                "4096",
                wrap_dev,
                CRYPT_NAME,
            ]
        )
        try:
            yield CRYPT_DEV
        finally:
            cmd(["cryptsetup", "close", CRYPT_NAME])


def setup_device(device):
    with plain_dmcrypt(device) as plain:
        cmd(["dd", "if=/dev/zero", f"of={plain}", f"bs=4M", "count=256"])


def main() -> int:
    with disk_image() as disk_img:
        with loopback_device(disk_img) as loop:
            setup_device(loop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
