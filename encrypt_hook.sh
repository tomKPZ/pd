#!/usr/bin/ash

run_hook() {
    modprobe -a -q dm-crypt
    udevadm settle
    dmsetup create dm-luks /boot_tmp/table.txt
    rm /boot_tmp/disk
    cryptsetup open --key-file=/boot_tmp/key.bin --keyfile-offset=64 --header=/boot_tmp/header.dat /dev/mapper/dm-luks luks
    shred -u /boot_tmp/*
}
