#!/usr/bin/env python3

import base64
import contextlib
import math
import os
import subprocess
import sys
import tempfile
import uuid

DISK_SIZE = 1024 * 1024 * 1024
SWAP_SIZE_PE = 64 // 4
PASSWORD = "test"
META_SIZE = 128 * 1024 * 1024
MOVE_PES = False
DMSETUP_SECTOR_SIZE = 512


def cmd(*args, check=True, **kwargs):
    return subprocess.run(*args, check=check, **kwargs)


def cmd_output(*args, **kwargs):
    return cmd(*args, stdout=subprocess.PIPE, **kwargs).stdout.decode().strip()


def dd(**kwargs):
    DD_OPS = {
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
        if k in DD_OPS:
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
def device_view(wrap_dev, start, end):
    start_sector, start_rem = divmod(start, DMSETUP_SECTOR_SIZE)
    end_sector, end_rem = divmod(end, DMSETUP_SECTOR_SIZE)
    assert start_rem == 0 and end_rem == 0

    device_name = f"view_{uuid.uuid4()}"
    device_path = f"/dev/mapper/{device_name}"

    dmsetup_input = f"0 {end_sector-start_sector} linear {wrap_dev} {start_sector}"
    cmd(
        ["dmsetup", "create", os.path.basename(device_path)],
        input=dmsetup_input.encode(),
    )
    try:
        yield device_path
    finally:
        cmd(["dmsetup", "remove", device_path])


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


@contextlib.contextmanager
def detached_luks(wrap_dev, key_file, key_file_offset, header_file):
    luks_name = f"luks_test_{uuid.uuid4()}"
    luks_dev = f"/dev/mapper/{luks_name}"

    cmd(
        [
            "cryptsetup",
            "luksFormat",
            "--batch-mode",
            "--hash",
            "sha512",
            "--cipher",
            "aes-xts-plain64",
            "--key-size",
            "512",
            "--sector-size",
            "4096",
            "--key-file",
            key_file,
            "--keyfile-offset",
            key_file_offset,
            "--header",
            header_file,
            # Remove this for real impl
            "--pbkdf-force-iterations",
            "4",
            "--pbkdf-parallel",
            "4",
            wrap_dev,
        ],
    )
    cmd(
        [
            "cryptsetup",
            "open",
            "--key-file",
            key_file,
            "--keyfile-offset",
            key_file_offset,
            "--header",
            header_file,
            wrap_dev,
            luks_name,
        ],
    )
    try:
        yield luks_dev
    finally:
        cmd(["cryptsetup", "close", luks_name])


@contextlib.contextmanager
def lvm(wrap_dev):
    lv_name = "test_lv"
    vg_name = f"system_{uuid.uuid4()}"
    swap_name = "swap"
    swap_dev = f"/dev/{vg_name}/{swap_name}"
    thinpool_name = "thinpool"
    thinpool_dev = f"/dev/{vg_name}/{thinpool_name}"
    lv_dev = f"/dev/{vg_name}/{lv_name}"

    dd(_if="/dev/zero", of=wrap_dev, bs="4M", count=1)
    cmd(["pvcreate", wrap_dev])
    try:
        cmd(
            [
                "vgcreate",
                "--physicalextentsize",
                "4m",
                vg_name,
                wrap_dev,
            ]
        )
        try:
            cmd(
                [
                    "lvcreate",
                    "--type",
                    "linear",
                    "--size",
                    f"{4 * SWAP_SIZE_PE}m",
                    "--name",
                    swap_name,
                    vg_name,
                ]
            )
            try:
                dd(_if="/dev/zero", of=swap_dev, bs="4M", check=False)
                cmd(["mkswap", swap_dev])
                thin_size_str = cmd_output(
                    [
                        "thin_metadata_size",
                        "--block-size",
                        "64k",
                        "--pool-size",
                        f"{DISK_SIZE}b",
                        "--max-thins",
                        "1024",
                        "-u",
                        "m",
                        "-n",
                    ]
                )
                thin_meta_pe = (math.ceil(float(thin_size_str)) + 3) // 4
                thin_meta_m = 4 * thin_meta_pe
                cmd(
                    [
                        "lvcreate",
                        "--type",
                        "thin-pool",
                        "--thinpool",
                        thinpool_name,
                        "--chunksize",
                        "64k",
                        "--poolmetadatasize",
                        f"{thin_meta_m}m",
                        "--size",
                        "4m",
                        vg_name,
                    ]
                )
                try:

                    def move(src, dst, size):
                        if not MOVE_PES:
                            return
                        cmd(
                            [
                                "pvmove",
                                "--alloc",
                                "anywhere",
                                f"{wrap_dev}:{SWAP_SIZE_PE + src}-{SWAP_SIZE_PE + src + size - 1}",
                                f"{wrap_dev}:{SWAP_SIZE_PE + dst}-{SWAP_SIZE_PE + dst + size - 1}",
                            ]
                        )

                    move(thin_meta_pe + 1, 2 * thin_meta_pe + 1, thin_meta_pe)
                    move(thin_meta_pe, 3 * thin_meta_pe + 1, 1)
                    move(2 * thin_meta_pe + 1, thin_meta_pe, thin_meta_pe)
                    move(3 * thin_meta_pe + 1, 2 * thin_meta_pe, 1)
                    cmd(["lvextend", "-l+100%FREE", thinpool_dev])
                    pool_size = cmd_output(
                        [
                            "lvs",
                            "--units",
                            "m",
                            "--noheadings",
                            "-o",
                            "lv_size",
                            thinpool_dev,
                        ]
                    ).split()[0]
                    cmd(
                        [
                            "lvcreate",
                            "--thin",
                            thinpool_dev,
                            "--name",
                            lv_name,
                            "--virtualsize",
                            pool_size,
                        ]
                    )
                    try:
                        cmd(["pvdisplay", "-v", "-m"])
                        yield lv_dev
                    finally:
                        cmd(["lvremove", "-f", lv_dev], check=False)
                finally:
                    cmd(["lvremove", "-f", thinpool_dev], check=False)
            finally:
                cmd(["lvremove", "-f", swap_dev], check=False)
        finally:
            cmd(["vgremove", "-f", vg_name], check=False)
    finally:
        cmd(["pvremove", "-f", wrap_dev], check=False)


@contextlib.contextmanager
def btrfs_mount(wrap_dev):
    mount_name = f"btrfs_mount_{uuid.uuid4()}"
    mount_point = f"/mnt/{mount_name}"

    cmd(["mkfs.btrfs", wrap_dev, "--csum=sha256", "--force", "--nodiscard"])
    os.mkdir(mount_point)
    try:
        cmd(
            [
                "mount",
                wrap_dev,
                mount_point,
                "-o",
                "defaults,ssd,nodiscard,compress=zstd:3",
            ]
        )
        try:
            yield mount_point
        finally:
            cmd(["umount", mount_point], check=False)
    finally:
        os.rmdir(mount_point)


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
        with device_view(device, 8 * 1024, META_SIZE) as meta_dev:
            with plain_dmcrypt(meta_dev, key_file) as meta_enc:
                dd(_if="/dev/zero", of=meta_enc, bs="4M", check=False)
                with device_view(meta_enc, 4 * 1024, META_SIZE - 8 * 1024) as meta_part:
                    with btrfs_mount(meta_part) as btrfs_meta:
                        with device_view(
                            device, META_SIZE, DISK_SIZE - META_SIZE
                        ) as data_part:
                            with detached_luks(
                                data_part, key_file, "64", f"{btrfs_meta}/header.dat"
                            ) as data:
                                with lvm(data) as lv:
                                    with btrfs_mount(lv) as btrfs_data:
                                        cmd(["mkdir", f"{btrfs_data}/boot"])


def main() -> int:
    with disk_image() as disk_img:
        with loopback_device(disk_img) as loop:
            setup_device(loop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
