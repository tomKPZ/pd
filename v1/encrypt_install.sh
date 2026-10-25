#!/bin/bash

build() {
    map add_module 'dm-crypt' 'dm-integrity'
    add_all_modules '/crypto/'

    add_binary 'cryptsetup'
    add_binary 'shred'

    map add_udev_rule \
        '10-dm.rules' \
        '13-dm-disk.rules' \
        '95-dm-notify.rules' \
        '/usr/lib/initcpio/udev/11-dm-initramfs.rules'

    # cryptsetup calls pthread_create(), which dlopen()s libgcc_s.so.1
    add_binary '/usr/lib/libgcc_s.so.1'

    add_runscript
}

help() {
    echo 'Initialize block encryption'
}
