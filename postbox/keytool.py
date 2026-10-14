#!/usr/bin/env python3
"""Key tool for the post box login. install.py runs it with the post box's own python environment.

  keytool.py ensure SEED_FILE   create this Mac's key pair when it has none (seed file mode 600) and print the PUBLIC key
  keytool.py pub SEED_FILE      print the public key of an existing seed file
  keytool.py check KEY          exit 0 when KEY is shaped like a public key (U plus 55 characters), else 1

The seed (the private key) is never printed. The public key is safe to share: it is what another Mac needs to accept this one.
"""
from __future__ import annotations

import os
import re
import sys

PUBLIC_KEY = re.compile(r"^U[A-Z2-7]{55}$")


def public_from_seed(seed_text):
    import nkeys
    return nkeys.from_seed(seed_text.strip().encode()).public_key.decode()


def ensure(path):
    if not os.path.exists(path):
        import nkeys
        seed = nkeys.encode_seed(os.urandom(32), nkeys.PREFIX_BYTE_USER)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(seed + b"\n")
    os.chmod(path, 0o600)   # tighten a file that was created some other way
    with open(path) as fh:
        return public_from_seed(fh.read())


def main(argv):
    if len(argv) == 3 and argv[1] in ("ensure", "pub"):
        try:
            if argv[1] == "pub" and not os.path.exists(argv[2]):
                raise FileNotFoundError(argv[2])
            print(ensure(argv[2]) if argv[1] == "ensure" else public_from_seed(open(argv[2]).read()))
        except Exception as exc:  # noqa: BLE001 - one plain line, never the key material
            print(f"keytool: cannot use the key file {argv[2]}: {type(exc).__name__}", file=sys.stderr)
            return 2
        return 0
    if len(argv) == 3 and argv[1] == "check":
        return 0 if PUBLIC_KEY.match(argv[2].strip()) else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
