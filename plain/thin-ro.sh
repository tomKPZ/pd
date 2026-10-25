# Sourced by late.sh.  Map a thin LV read-only *without* activating its thin
# pool.  dm-thin can't run in a read-only table, and activating it
# read-write could write pool metadata, so instead: read the pool's mapping
# tree with thin_dump and build an equivalent dm-linear table over the
# pool's data, with unmapped blocks reading as zeros.  Nothing is written.

# lv_table VG LV DEV: dm-linear table for LV's segments on the PV DEV.
lv_table() {
    lvm lvs --readonly --devices "$3" --noheadings --nosuffix --units s -a \
        -o seg_start,seg_size,seg_pe_ranges,vg_extent_size "$1/$2" 2>/dev/null |
        awk -v pe_start="$pe_start" -v dev="$3" '{
            split($3, r, ":"); split(r[2], e, "-")
            printf "%d %d linear %s %d\n", $1, $2, dev, pe_start + e[1] * $4
        }'
}

# thin_ro_open VG LV NAME: create /dev/mapper/NAME, a read-only view of VG/LV.
thin_ro_open() {
    vg=$1 lv=$2 name=$3 pv=/dev/mapper/cryptlvm
    l="lvm lvs --readonly --devices $pv --noheadings"
    pe_start=$(lvm pvs --readonly --devices "$pv" --noheadings --nosuffix --units s -o pe_start "$pv" 2>/dev/null | tr -d ' ')
    pool=$($l -o pool_lv "$vg/$lv" 2>/dev/null | tr -d ' ')
    id=$($l -o thin_id "$vg/$lv" 2>/dev/null | tr -d ' ')
    size=$($l --nosuffix --units s -o lv_size "$vg/$lv" 2>/dev/null | tr -d ' ')
    [ -n "$pe_start" ] && [ -n "$pool" ] && [ -n "$id" ] && [ -n "$size" ] || return 1

    lv_table "$vg" "${pool}_tmeta" "$pv" | dmsetup create --readonly ro-tmeta
    lv_table "$vg" "${pool}_tdata" "$pv" | dmsetup create --readonly ro-tdata
    dmsetup mknodes

    thin_dump --dev-id "$id" /dev/mapper/ro-tmeta |
        awk -v size="$size" -v tdata=/dev/mapper/ro-tdata '
            function attr(name,   m) {
                if (match($0, name "=\"[0-9]+\"")) {
                    m = substr($0, RSTART, RLENGTH); sub(/^[^"]*"/, "", m); sub(/"$/, "", m)
                    return m + 0
                }
                return -1
            }
            function emit(vstart, len, dstart) {
                if (vstart > pos) printf "%d %d zero\n", pos, vstart - pos
                printf "%d %d linear %s %d\n", vstart, len, tdata, dstart
                pos = vstart + len
            }
            /<superblock/ { bs = attr("data_block_size") }
            /<range_mapping/ {
                emit(attr("origin_begin") * bs, attr("length") * bs, attr("data_begin") * bs)
            }
            /<single_mapping/ { emit(attr("origin_block") * bs, bs, attr("data_block") * bs) }
            END {
                if (pos < size) printf "%d %d zero\n", pos, size - pos
            }
            BEGIN { pos = 0 }
        ' | dmsetup create --readonly "$name"
    dmsetup mknodes
}

