"""Root pytest configuration: run the whole suite with excerpt encryption switched on."""

import os

from cryptography.fernet import Fernet

# Real behaviour in every DB test: excerpts are encrypted when written and decrypted on read.
os.environ.setdefault("EXCERPTS_ENCRYPTION_KEYS", Fernet.generate_key().decode())
