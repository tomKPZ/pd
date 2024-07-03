#!/usr/bin/env python3

import atexit
import os
import shutil
import subprocess
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
ROOT = tempfile.mkdtemp()


def cleanup():
    shutil.rmtree(ROOT)


atexit.register(cleanup)


def add_file(file):
    dst = os.path.join(ROOT, file.lstrip("/"))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy(file, dst)


def add_library(lib):
    dst = os.path.join(ROOT, lib.lstrip("/"))
    if not os.path.isfile(dst):
        add_file(lib)
        subprocess.run(["strip", "--strip-unneeded", dst], check=True)


def add_binary(bin):
    add_file(bin)
    dst = os.path.join(ROOT, bin.lstrip("/"))
    subprocess.run(["strip", "--strip-all", dst], check=True)

    links = subprocess.run(
        ["find", "-L", os.path.dirname(bin), "-samefile", bin, "-xtype", "l"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()

    for link in links:
        link_path = os.path.join(ROOT, link.lstrip("/"))
        os.makedirs(os.path.dirname(link_path), exist_ok=True)
        os.symlink(os.path.basename(bin), link_path)

    dependencies = subprocess.run(
        ["ldd", bin], capture_output=True, text=True, check=False
    ).stdout.splitlines()

    for dep in dependencies:
        dep_path = (
            dep.split()[2]
            if len(dep.split()) > 2 and dep.split()[2].startswith("/")
            else None
        )
        if dep_path:
            add_library(dep_path)


os.makedirs(os.path.join(ROOT, "proc"), exist_ok=True)
os.makedirs(os.path.join(ROOT, "sys"), exist_ok=True)
os.makedirs(os.path.join(ROOT, "dev"), exist_ok=True)
os.makedirs(os.path.join(ROOT, "run"), exist_ok=True)
os.makedirs(os.path.join(ROOT, "tmp"), exist_ok=True)
os.makedirs(os.path.join(ROOT, "var"), exist_ok=True)
os.makedirs(os.path.join(ROOT, "etc"), exist_ok=True)
os.makedirs(os.path.join(ROOT, "usr", "lib"), exist_ok=True)
os.makedirs(os.path.join(ROOT, "usr", "bin"), exist_ok=True)
os.symlink("usr/lib", os.path.join(ROOT, "lib"))
os.symlink("bin", os.path.join(ROOT, "usr", "sbin"))
os.symlink("usr/bin", os.path.join(ROOT, "bin"))
os.symlink("usr/bin", os.path.join(ROOT, "sbin"))
os.symlink("../run", os.path.join(ROOT, "var", "run"))
os.symlink("lib", os.path.join(ROOT, "usr", "lib64"))
os.symlink("usr/lib", os.path.join(ROOT, "lib64"))

bins_to_add = [
    "/usr/bin/argon2",
    "/usr/bin/cryptsetup",
    "/usr/bin/dmsetup",
    "/usr/bin/integritysetup",
    "/usr/bin/kexec",
    "/usr/bin/lvm",
    "/usr/bin/pdata_tools",
]

for bin in bins_to_add:
    add_binary(bin)

add_library("/usr/lib/ossl-modules/legacy.so")
add_binary("/usr/bin/busybox")

applets = subprocess.run(
    ["/usr/bin/busybox", "--list"], capture_output=True, text=True, check=True
).stdout.splitlines()

for applet in applets:
    if applet != "busybox":
        os.symlink("busybox", os.path.join(ROOT, "usr", "bin", applet))

os.symlink("../proc/self/mounts", os.path.join(ROOT, "etc", "mtab"))
open(os.path.join(ROOT, "etc", "fstab"), "w").close()
open(os.path.join(ROOT, "etc", "ld.so.conf"), "w").close()

os.makedirs(os.path.join(ROOT, "etc", "lvm"), exist_ok=True)
open(os.path.join(ROOT, "etc", "lvm", "lvm.conf"), "w").close()

subprocess.run(["ldconfig", "-r", ROOT], check=True)

shutil.copy(os.path.join(SCRIPT_DIR, "early.sh"), ROOT)
shutil.copy(os.path.join(SCRIPT_DIR, "init"), ROOT)

subprocess.run(["tree", ROOT], check=True)
SAVE_DIR = os.getcwd()
subprocess.run(
    f"find . -print0 | cpio --null -ov --format=newc | gzip -9 >{SAVE_DIR}/archive.cpio.gz",
    cwd=ROOT,
    shell=True,
    check=True,
)
