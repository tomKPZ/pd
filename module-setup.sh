#!/bin/bash

depends() {
    echo dm rootfs-block
}

install() {
    inst_multiple cryptsetup shred dmsetup
    inst_hook initqueue/settled 50 "$moddir/encrypt.sh"
}
