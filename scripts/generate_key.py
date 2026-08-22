#!/usr/bin/env python3
"""Generate a Fernet key for DB_ENCRYPTION_KEY."""

from cryptography.fernet import Fernet


if __name__ == "__main__":
    print(Fernet.generate_key().decode("ascii"))

