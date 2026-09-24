#!/usr/bin/python3
"""Installed local Ollama reviewer. Run only in the review sandbox."""

import json
import socket
import sys
from pathlib import Path


def main():
    context = json.loads(Path("/context.json").read_text())
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect("/scratch/inference.sock")
        connection.sendall(context["sha256"].encode("ascii"))
        connection.shutdown(socket.SHUT_WR)
        output = bytearray()
        while True:
            block = connection.recv(4096)
            if not block:
                break
            output.extend(block)
            if len(output) > 16 * 1024:
                raise ValueError("review response exceeds output cap")
    if not output:
        raise ValueError("local Ollama returned no review")
    sys.stdout.buffer.write(output)


if __name__ == "__main__":
    main()
