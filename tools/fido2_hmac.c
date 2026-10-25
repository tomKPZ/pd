/*
 * fido2-hmac: the FIDO2 hmac-secret operations pd needs (docs/design.md §4.1).
 *
 *   fido2-hmac list                     FIDO hidraw devices, one per line
 *   fido2-hmac setpin DEV               set the PIN (new PIN on stdin; tests)
 *   fido2-hmac make DEV                 new credential (PIN on stdin); prints its ID (hex)
 *   fido2-hmac has DEV CRED             exit 0 if CRED (hex) belongs to DEV (no touch)
 *   fido2-hmac get DEV CRED SALT        hmac-secret for SALT (32 bytes, hex); PIN on stdin
 *   fido2-hmac unlock SLOTS KEY OUTDIR  boot: see below
 *
 * Credentials are non-resident (nothing is stored on the token), with the
 * hmac-secret extension.  Like systemd-cryptenroll's defaults, every
 * operation needs the PIN and a touch, and doesn't ask for user verification.
 *
 * unlock: SLOTS has one line per credential, "CRED SLOT0 SLOT1" (hex, slots
 * 64 bytes); KEY is the Argon2 output (64 bytes).  Waits for a token holding
 * one of the credentials, asks it for h = hmac-secret(SHA-256(KEY)), and
 * writes OUTDIR/slot0.key and OUTDIR/slot1.key = SLOTn XOR SHA-512(KEY || h).
 *
 * Exit codes: 0 ok, 1 error, 2 no token found, 3 wrong PIN, 4 PIN blocked
 * (remove and reinsert the token, or it's locked), 5 no touch.
 */

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <fido.h>
#include <openssl/sha.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define RP_ID "pd"
#define MAX_DEVS 16
#define MAX_CREDS 64
#define MAX_CRED_LEN 1024
#define KEY_LEN 64
#define SLOTS 2
#define WAIT_SECONDS 60

static int exit_code(int r)
{
    switch (r) {
    case FIDO_OK:
        return 0;
    case FIDO_ERR_PIN_INVALID:
        return 3;
    case FIDO_ERR_PIN_BLOCKED:
    case FIDO_ERR_PIN_AUTH_BLOCKED:
        return 4;
    case FIDO_ERR_ACTION_TIMEOUT:
    case FIDO_ERR_USER_ACTION_TIMEOUT:
        return 5;
    default:
        return 1;
    }
}

static void die_fido(const char *what, int r)
{
    fprintf(stderr, "fido2-hmac: %s: %s\n", what, fido_strerr(r));
    exit(exit_code(r));
}

static void die(const char *msg)
{
    fprintf(stderr, "fido2-hmac: %s\n", msg);
    exit(1);
}

static size_t unhex(const char *s, unsigned char *out, size_t max)
{
    size_t n = strlen(s);
    if (n % 2 || n / 2 > max)
        die("bad hex argument");
    for (size_t i = 0; i < n / 2; i++) {
        unsigned v;
        if (sscanf(s + 2 * i, "%2x", &v) != 1)
            die("bad hex argument");
        out[i] = (unsigned char)v;
    }
    return n / 2;
}

static void print_hex(const unsigned char *p, size_t n)
{
    for (size_t i = 0; i < n; i++)
        printf("%02x", p[i]);
    printf("\n");
}

/* The PIN, from stdin (one line). */
static char *read_pin(void)
{
    static char pin[256];
    if (!fgets(pin, sizeof pin, stdin))
        die("no PIN on stdin");
    pin[strcspn(pin, "\n")] = 0;
    if (!pin[0])
        die("empty PIN");
    return pin;
}

static void wipe(void *p, size_t n)
{
    volatile unsigned char *v = p;
    while (n--)
        *v++ = 0;
}

/* Is this hidraw device a FIDO device?  Its report descriptor declares the
 * FIDO usage page (0xF1D0).  Checked through sysfs, so nothing is ever sent
 * to other HID devices (keyboards). */
static int is_fido_hidraw(const char *name)
{
    char path[256];
    unsigned char d[4096];
    snprintf(path, sizeof path, "/sys/class/hidraw/%s/device/report_descriptor", name);
    int fd = open(path, O_RDONLY);
    if (fd < 0)
        return 0;
    ssize_t n = read(fd, d, sizeof d);
    close(fd);
    for (ssize_t i = 0; i + 2 < n; i++)
        if (d[i] == 0x06 && d[i + 1] == 0xd0 && d[i + 2] == 0xf1)
            return 1;
    return 0;
}

static int list_devices(char devs[][64])
{
    int n = 0;
    DIR *dir = opendir("/sys/class/hidraw");
    if (!dir)
        return 0;
    struct dirent *e;
    while ((e = readdir(dir)) && n < MAX_DEVS) {
        if (strncmp(e->d_name, "hidraw", 6) || !is_fido_hidraw(e->d_name))
            continue;
        snprintf(devs[n++], 64, "/dev/%s", e->d_name);
    }
    closedir(dir);
    return n;
}

static fido_dev_t *open_dev(const char *path)
{
    fido_dev_t *dev = fido_dev_new();
    if (!dev)
        die("out of memory");
    int r = fido_dev_open(dev, path);
    if (r != FIDO_OK) {
        fido_dev_free(&dev);
        return NULL;
    }
    return dev;
}

static void close_dev(fido_dev_t **dev)
{
    fido_dev_close(*dev);
    fido_dev_free(dev);
}

static void random_bytes(unsigned char *p, size_t n)
{
    int fd = open("/dev/urandom", O_RDONLY);
    if (fd < 0 || read(fd, p, n) != (ssize_t)n)
        die("can't read /dev/urandom");
    close(fd);
}

/* Does the credential belong to this token?  A silent assertion (no PIN,
 * no touch): the token answers "no credentials" for IDs it didn't create. */
static int has_cred(fido_dev_t *dev, const unsigned char *id, size_t len)
{
    unsigned char cdh[32];
    random_bytes(cdh, sizeof cdh);
    fido_assert_t *a = fido_assert_new();
    if (!a)
        die("out of memory");
    int ok = fido_assert_set_clientdata_hash(a, cdh, sizeof cdh) == FIDO_OK &&
             fido_assert_set_rp(a, RP_ID) == FIDO_OK &&
             fido_assert_allow_cred(a, id, len) == FIDO_OK &&
             fido_assert_set_up(a, FIDO_OPT_FALSE) == FIDO_OK &&
             fido_dev_get_assert(dev, a, NULL) == FIDO_OK;
    fido_assert_free(&a);
    return ok;
}

static void get_hmac(fido_dev_t *dev, const unsigned char *id, size_t len,
                     const unsigned char salt[32], const char *pin, unsigned char out[32])
{
    unsigned char cdh[32];
    random_bytes(cdh, sizeof cdh);
    fido_assert_t *a = fido_assert_new();
    if (!a)
        die("out of memory");
    int r;
    if ((r = fido_assert_set_clientdata_hash(a, cdh, sizeof cdh)) != FIDO_OK ||
        (r = fido_assert_set_rp(a, RP_ID)) != FIDO_OK ||
        (r = fido_assert_allow_cred(a, id, len)) != FIDO_OK ||
        (r = fido_assert_set_extensions(a, FIDO_EXT_HMAC_SECRET)) != FIDO_OK ||
        (r = fido_assert_set_hmac_salt(a, salt, 32)) != FIDO_OK ||
        (r = fido_assert_set_up(a, FIDO_OPT_TRUE)) != FIDO_OK ||
        (r = fido_assert_set_uv(a, FIDO_OPT_OMIT)) != FIDO_OK)
        die_fido("assertion setup", r);
    fprintf(stderr, "Touch your security key.\n");
    if ((r = fido_dev_get_assert(dev, a, pin)) != FIDO_OK)
        die_fido("assertion", r);
    if (fido_assert_count(a) != 1 || fido_assert_hmac_secret_len(a, 0) != 32)
        die("the security key returned no hmac-secret");
    memcpy(out, fido_assert_hmac_secret_ptr(a, 0), 32);
    fido_assert_free(&a);
}

static int supports_hmac_secret(fido_dev_t *dev)
{
    fido_cbor_info_t *ci = fido_cbor_info_new();
    int found = 0;
    if (ci && fido_dev_get_cbor_info(dev, ci) == FIDO_OK) {
        char **ext = fido_cbor_info_extensions_ptr(ci);
        for (size_t i = 0; i < fido_cbor_info_extensions_len(ci); i++)
            if (!strcmp(ext[i], "hmac-secret"))
                found = 1;
    }
    fido_cbor_info_free(&ci);
    return found;
}

static int cmd_list(void)
{
    char devs[MAX_DEVS][64];
    int n = list_devices(devs);
    for (int i = 0; i < n; i++)
        printf("%s\n", devs[i]);
    return 0;
}

static int cmd_setpin(const char *path)
{
    fido_dev_t *dev = open_dev(path);
    if (!dev)
        die("can't open the security key");
    char *pin = read_pin();
    int r = fido_dev_set_pin(dev, pin, NULL);
    wipe(pin, strlen(pin));
    if (r != FIDO_OK)
        die_fido("set PIN", r);
    close_dev(&dev);
    return 0;
}

static int cmd_make(const char *path)
{
    fido_dev_t *dev = open_dev(path);
    if (!dev)
        die("can't open the security key");
    if (!fido_dev_is_fido2(dev) || !supports_hmac_secret(dev))
        die("the security key doesn't support FIDO2 hmac-secret");
    if (!fido_dev_has_pin(dev))
        die("the security key has no PIN; set one first (e.g. `ykman fido access change-pin`)");
    char *pin = read_pin();
    unsigned char cdh[32], user[32];
    random_bytes(cdh, sizeof cdh);
    random_bytes(user, sizeof user);
    fido_cred_t *c = fido_cred_new();
    if (!c)
        die("out of memory");
    int r;
    if ((r = fido_cred_set_type(c, COSE_ES256)) != FIDO_OK ||
        (r = fido_cred_set_clientdata_hash(c, cdh, sizeof cdh)) != FIDO_OK ||
        (r = fido_cred_set_rp(c, RP_ID, NULL)) != FIDO_OK ||
        (r = fido_cred_set_user(c, user, sizeof user, RP_ID, NULL, NULL)) != FIDO_OK ||
        (r = fido_cred_set_extensions(c, FIDO_EXT_HMAC_SECRET)) != FIDO_OK ||
        (r = fido_cred_set_rk(c, FIDO_OPT_FALSE)) != FIDO_OK ||
        (r = fido_cred_set_uv(c, FIDO_OPT_OMIT)) != FIDO_OK)
        die_fido("credential setup", r);
    fprintf(stderr, "Touch your security key.\n");
    r = fido_dev_make_cred(dev, c, pin);
    wipe(pin, strlen(pin));
    if (r != FIDO_OK)
        die_fido("make credential", r);
    print_hex(fido_cred_id_ptr(c), fido_cred_id_len(c));
    fido_cred_free(&c);
    close_dev(&dev);
    return 0;
}

static int cmd_has(const char *path, const char *cred)
{
    unsigned char id[MAX_CRED_LEN];
    size_t len = unhex(cred, id, sizeof id);
    fido_dev_t *dev = open_dev(path);
    if (!dev)
        die("can't open the security key");
    int ok = has_cred(dev, id, len);
    close_dev(&dev);
    return ok ? 0 : 1;
}

static int cmd_get(const char *path, const char *cred, const char *salthex)
{
    unsigned char id[MAX_CRED_LEN], salt[32], h[32];
    size_t len = unhex(cred, id, sizeof id);
    if (unhex(salthex, salt, sizeof salt) != 32)
        die("the salt must be 32 bytes");
    fido_dev_t *dev = open_dev(path);
    if (!dev)
        die("can't open the security key");
    char *pin = read_pin();
    get_hmac(dev, id, len, salt, pin, h);
    wipe(pin, strlen(pin));
    print_hex(h, sizeof h);
    wipe(h, sizeof h);
    close_dev(&dev);
    return 0;
}

struct cred {
    unsigned char id[MAX_CRED_LEN];
    size_t len;
    unsigned char slot[SLOTS][KEY_LEN];
};

static int read_slots(const char *path, struct cred *creds)
{
    FILE *f = fopen(path, "r");
    if (!f)
        die("can't read the slots file");
    char line[4 * MAX_CRED_LEN];
    int n = 0;
    while (fgets(line, sizeof line, f) && n < MAX_CREDS) {
        char *id = strtok(line, " \t\n");
        if (!id || id[0] == '#')
            continue;
        creds[n].len = unhex(id, creds[n].id, MAX_CRED_LEN);
        for (int s = 0; s < SLOTS; s++) {
            char *hex = strtok(NULL, " \t\n");
            if (!hex || unhex(hex, creds[n].slot[s], KEY_LEN) != KEY_LEN)
                die("bad slots file");
        }
        n++;
    }
    fclose(f);
    return n;
}

static void write_key(const char *dir, int slot, const unsigned char *k)
{
    char path[512];
    snprintf(path, sizeof path, "%s/slot%d.key", dir, slot);
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0 || write(fd, k, KEY_LEN) != KEY_LEN || close(fd) != 0)
        die("can't write a key file");
}

static int cmd_unlock(const char *slots, const char *keyfile, const char *outdir)
{
    static struct cred creds[MAX_CREDS];
    int ncreds = read_slots(slots, creds);
    unsigned char key[KEY_LEN];
    int fd = open(keyfile, O_RDONLY);
    if (fd < 0 || read(fd, key, KEY_LEN) != KEY_LEN)
        die("can't read the key file");
    close(fd);

    /* Wait for a token that holds one of the credentials. */
    fido_dev_t *dev = NULL;
    int which = -1;
    int told = 0;
    time_t end = time(NULL) + WAIT_SECONDS;
    while (which < 0) {
        char devs[MAX_DEVS][64];
        int n = list_devices(devs);
        for (int i = 0; i < n && which < 0; i++) {
            if (!(dev = open_dev(devs[i])))
                continue;
            for (int c = 0; c < ncreds && which < 0; c++)
                if (has_cred(dev, creds[c].id, creds[c].len))
                    which = c;
            if (which < 0)
                close_dev(&dev);
        }
        if (which >= 0)
            break;
        if (time(NULL) >= end) {
            fprintf(stderr, "No security key found.\n");
            return 2;
        }
        if (!told) {
            fprintf(stderr, "Insert your security key.\n");
            told = 1;
        }
        usleep(250000);
    }

    unsigned char salt[32], h[32], w[SHA512_DIGEST_LENGTH], buf[KEY_LEN + 32], k[KEY_LEN];
    SHA256(key, KEY_LEN, salt);
    char *pin = read_pin();
    get_hmac(dev, creds[which].id, creds[which].len, salt, pin, h);
    wipe(pin, strlen(pin));
    close_dev(&dev);
    memcpy(buf, key, KEY_LEN);
    memcpy(buf + KEY_LEN, h, 32);
    SHA512(buf, sizeof buf, w);
    for (int s = 0; s < SLOTS; s++) {
        for (int i = 0; i < KEY_LEN; i++)
            k[i] = creds[which].slot[s][i] ^ w[i];
        write_key(outdir, s, k);
    }
    wipe(key, sizeof key);
    wipe(h, sizeof h);
    wipe(w, sizeof w);
    wipe(buf, sizeof buf);
    wipe(k, sizeof k);
    return 0;
}

int main(int argc, char **argv)
{
    fido_init(0);
    if (argc == 2 && !strcmp(argv[1], "list"))
        return cmd_list();
    if (argc == 3 && !strcmp(argv[1], "setpin"))
        return cmd_setpin(argv[2]);
    if (argc == 3 && !strcmp(argv[1], "make"))
        return cmd_make(argv[2]);
    if (argc == 4 && !strcmp(argv[1], "has"))
        return cmd_has(argv[2], argv[3]);
    if (argc == 5 && !strcmp(argv[1], "get"))
        return cmd_get(argv[2], argv[3], argv[4]);
    if (argc == 5 && !strcmp(argv[1], "unlock"))
        return cmd_unlock(argv[2], argv[3], argv[4]);
    fprintf(stderr, "usage: fido2-hmac list | setpin DEV | make DEV | has DEV CRED | "
                    "get DEV CRED SALT | unlock SLOTS KEY OUTDIR\n");
    return 1;
}
