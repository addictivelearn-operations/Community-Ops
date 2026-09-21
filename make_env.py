"""
Creates .env from .env.example, filling in what can be filled automatically:

  • ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET / ZOHO_REFRESH_TOKEN — copied from
    ../zoho-token-bridge/.env (the same Self Client), if that file exists.
  • APP_SECRET — a fresh random value.

Nothing is printed except which keys were filled. Run once:
    python make_env.py
Then open .env and add GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET by hand.
"""

import secrets
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXAMPLE = HERE / ".env.example"
TARGET = HERE / ".env"
BRIDGE_ENV = HERE.parent / "zoho-token-bridge" / ".env"


def read_env(path: Path) -> dict:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip("'\"")
    return out


def main() -> None:
    if TARGET.exists():
        print(".env already exists — not touching it. Delete it first to regenerate.")
        return
    bridge = read_env(BRIDGE_ENV)
    fill = {"APP_SECRET": secrets.token_urlsafe(48)}
    for key in ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN"):
        if bridge.get(key):
            fill[key] = bridge[key]

    lines = []
    for line in EXAMPLE.read_text(encoding="utf-8").splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line and not line.startswith("#") else None
        if key in fill:
            lines.append(f"{key}={fill[key]}")
        else:
            lines.append(line)
    TARGET.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("Wrote .env")
    print("  filled  :", ", ".join(sorted(fill)))
    missing = [k for k in ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN") if k not in fill]
    if missing:
        print("  NOT found in the token bridge .env, add by hand:", ", ".join(missing))
    print("  still needed: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET (see README step 1)")


if __name__ == "__main__":
    main()
