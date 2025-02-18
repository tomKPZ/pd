#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/input.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#define INPUT_DIR "/dev/input/"
#define EVENT_PREFIX "event"

int check_shift_state(const char *device) {
  int fd = open(device, O_RDONLY);
  if (fd < 0) {
    perror("Cannot open device");
    return -1; // Continue checking other devices
  }

  unsigned char key_states[KEY_MAX / 8 + 1] = {0};

  if (ioctl(fd, EVIOCGKEY(sizeof(key_states)), key_states) < 0) {
    perror("ioctl EVIOCGKEY failed");
    close(fd);
    return -1;
  }

  close(fd);

  int left_shift = key_states[KEY_LEFTSHIFT / 8] & (1 << (KEY_LEFTSHIFT % 8));
  int right_shift =
      key_states[KEY_RIGHTSHIFT / 8] & (1 << (KEY_RIGHTSHIFT % 8));

  return left_shift || right_shift;
}

int scan_input_devices() {
  struct dirent *entry;
  DIR *dir = opendir(INPUT_DIR);
  if (!dir) {
    perror("Cannot open /dev/input/");
    return 2;
  }

  char device_path[PATH_MAX] = "";
  while ((entry = readdir(dir))) {
    if (strncmp(entry->d_name, EVENT_PREFIX, strlen(EVENT_PREFIX)) == 0) {
      if (snprintf(device_path, sizeof(device_path), "%s%s", INPUT_DIR,
                   entry->d_name) >= (int)sizeof(device_path)) {
        fprintf(stderr, "Device path truncated: %s%s\n", INPUT_DIR,
                entry->d_name);
        continue;
      }

      if (check_shift_state(device_path) == 1) {
        closedir(dir);
        return 0;
      }
    }
  }

  closedir(dir);
  return 1; // Shift is not held down
}

int main() { return scan_input_devices(); }
