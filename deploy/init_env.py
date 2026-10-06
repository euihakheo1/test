"""Create private configuration without printing secrets or overwriting an existing file.

Run from the repository root through uv. The generated files are excluded from Git.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("local", "prod"))
    parser.add_argument("--domain", help="prod HTTPS hostname, without scheme or path")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.mode == "local":
        output = root / ".env.vllm"
        text = (root / ".env.vllm.example").read_text(encoding="utf-8")
        text = text.replace("change-me-local-inference-key", secrets.token_urlsafe(48))
    else:
        if not args.domain or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", args.domain):
            parser.error("prod requires --domain with a DNS hostname")
        output = root / "deploy" / ".env"
        text = (root / "deploy" / ".env.example").read_text(encoding="utf-8")
        text = text.replace("change-me-postgres-password", secrets.token_urlsafe(36))
        text = text.replace("change-me-generate-a-random-48-byte-secret", secrets.token_urlsafe(48))
        text = text.replace("https://app.example.com", f"https://{args.domain}")
        text += f"\nJETTAE_DOMAIN={args.domain}\nJETTAE_VLLM_API_KEY={secrets.token_urlsafe(48)}\n"
    # Exclusive creation prevents accidental loss of a deployment's existing keys.
    try:
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        parser.exit(1, f"{output.name} already exists; keeping existing keys.\n")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
    print(f"Created {output.name}; secret values were not printed.")


if __name__ == "__main__":
    main()
