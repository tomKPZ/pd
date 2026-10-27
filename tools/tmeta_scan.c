/*
 * tmeta_scan: the highest data block that any btree leaf in a dm-thin pool's
 * metadata device maps, counting stale copies too.
 *
 * The pool's metadata is copy-on-write: superseded btree nodes stay in its
 * free blocks until they're reused, and still say where data used to be.
 * The frontier monitor (plain/hidden/frontier.sh) compares this with the
 * hidden side's space (docs/design.md §10.5): old decoy data there, if the
 * hidden side wrote over it, would show.
 *
 * Every 4 KiB block that is a valid btree leaf with 64-bit values counts: the
 * header's checksum (crc32c, as dm-persistent-data computes it) and its own
 * block number must match.  Bottom-level mapping values are
 * (data block << 24 | time); the top-level tree's values are metadata block
 * numbers, which read as data block 0 and so don't matter.  Prints the
 * highest data block plus one (0 if there is none), in data blocks.
 *
 * Usage: tmeta_scan DEVICE
 */
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define BLOCK 4096
#define HEADER 32
#define LEAF_NODE 2
#define BTREE_CSUM_XOR 121107
#define CHUNK (256 * BLOCK)

static uint32_t table[256];

static void crc_init(void) {
  for (uint32_t i = 0; i < 256; i++) {
    uint32_t c = i;
    for (int k = 0; k < 8; k++)
      c = (c & 1) ? (c >> 1) ^ 0x82F63B78u : c >> 1;
    table[i] = c;
  }
}

/* The kernel's crc32c(seed, ...): no inversion of its own. */
static uint32_t crc32c_raw(uint32_t crc, const uint8_t *p, size_t n) {
  while (n--)
    crc = table[(crc ^ *p++) & 0xff] ^ (crc >> 8);
  return crc;
}

static uint32_t le32(const uint8_t *p) {
  return p[0] | p[1] << 8 | p[2] << 16 | (uint32_t)p[3] << 24;
}

static uint64_t le64(const uint8_t *p) {
  return le32(p) | (uint64_t)le32(p + 4) << 32;
}

int main(int argc, char **argv) {
  if (argc != 2) {
    fprintf(stderr, "usage: tmeta_scan DEVICE\n");
    return 2;
  }
  int fd = open(argv[1], O_RDONLY);
  if (fd < 0) {
    perror(argv[1]);
    return 1;
  }
  crc_init();
  /* As dm-btree's calc_max_entries: 16 bytes per entry, a multiple of 3. */
  const uint32_t max_entries = (BLOCK - HEADER) / 16 / 3 * 3;
  static uint8_t buf[CHUNK];
  uint64_t blocknr = 0, top = 0, leaves = 0;
  for (;;) {
    ssize_t got = 0;
    while (got < CHUNK) {
      ssize_t n = read(fd, buf + got, CHUNK - got);
      if (n < 0 && errno == EINTR)
        continue;
      if (n < 0) {
        perror("read");
        return 1;
      }
      if (n == 0)
        break;
      got += n;
    }
    for (ssize_t off = 0; off + BLOCK <= got; off += BLOCK, blocknr++) {
      const uint8_t *b = buf + off;
      uint32_t flags = le32(b + 4), nr = le32(b + 16), max = le32(b + 20), vsize = le32(b + 24);
      if (flags != LEAF_NODE || vsize != 8 || max != max_entries || nr > max ||
          le64(b + 8) != blocknr)
        continue;
      if ((crc32c_raw(~0u, b + 4, BLOCK - 4) ^ BTREE_CSUM_XOR) != le32(b))
        continue;
      leaves++;
      const uint8_t *values = b + HEADER + (size_t)max * 8;
      for (uint32_t i = 0; i < nr; i++) {
        uint64_t block = le64(values + (size_t)i * 8) >> 24;
        if (block + 1 > top)
          top = block + 1;
      }
    }
    if (got < CHUNK)
      break;
  }
  close(fd);
  printf("%llu\n", (unsigned long long)top);
  fprintf(stderr, "tmeta_scan: %llu leaves in %llu blocks\n", (unsigned long long)leaves,
          (unsigned long long)blocknr);
  return 0;
}

