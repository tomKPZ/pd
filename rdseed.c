#include <stdint.h>
#include <stdio.h>
#include <x86intrin.h>

#define STEPS 8192

int main() {
  for (size_t i = 0; i < STEPS; i++) {
    unsigned long long random_value;
    if (!_rdseed64_step(&random_value)) {
      return 1;
    }
    fwrite(&random_value, sizeof(random_value), 1, stdout);
  }
  return 0;
}
