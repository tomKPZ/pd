#!/usr/bin/env python3 

import os
import mmap
import sys
from termcolor import colored

def is_chunk_matching(chunk, value):
    for i in range(0, len(chunk), 4):
        if int.from_bytes(chunk[i:i+4], byteorder='little') != value:
            return False
    return True

def main(file_path, chunk_size):
    value = 0x7dc2655a
    chunks_per_line = 64

    try:
        with open(file_path, 'rb') as f:
            file_size = os.path.getsize(file_path)
            num_chunks = (file_size + chunk_size - 1) // chunk_size
            
            with mmap.mmap(f.fileno(), length=0, access=mmap.ACCESS_READ) as mm:
                for chunk_idx in range(num_chunks):
                    chunk_start = chunk_idx * chunk_size
                    chunk_end = min(chunk_start + chunk_size, file_size)
                    chunk = mm[chunk_start:chunk_end]

                    if is_chunk_matching(chunk, value):
                        print(colored(' ', 'white', 'on_green'), end='')
                    else:
                        print(colored(' ', 'white', 'on_red'), end='')

                    if (chunk_idx + 1) % chunks_per_line == 0:
                        print()
                        
                # Print a newline if the last line is not complete
                if num_chunks % chunks_per_line != 0:
                    print()
    except FileNotFoundError:
        print(f"File {file_path} not found.")
        sys.exit(1)
    except Exception as e:
        print(f"An error occurred: {e}")
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <file_path> <chunk_size>")
        print(f"Example: {sys.argv[0]} disk.img 1048576")
        sys.exit(1)

    file_path = sys.argv[1]
    chunk_size = int(sys.argv[2])

    if (chunk_size & (chunk_size - 1)) != 0 or chunk_size <= 0:
        print("Chunk size must be a power of 2 and greater than 0.")
        sys.exit(1)

    main(file_path, chunk_size)

