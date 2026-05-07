#!/usr/bin/env python3
"""
One-time OAuth setup for YouTube uploads.

This is interactive - it opens a browser, you sign in to the YouTube account
that owns the WEB DESIGN AD channel, approve scope `youtube.upload`, and the
script writes credentials/token.json. Daily runs reuse that token (refreshing
silently when expired).

Usage:
    python3 oauth_setup.py
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CRED_DIR = ROOT / "credentials"
CLIENT_SECRETS = CRED_DIR / "client_secrets.json"
TOKEN_FILE = CRED_DIR / "token.json"

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def main():
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not CLIENT_SECRETS.exists():
        raise SystemExit(f"Missing {CLIENT_SECRETS}")

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS), SCOPES)
    # Spins up a tiny local server, opens the browser, captures the redirect.
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    print(f"Saved {TOKEN_FILE}")
    print("You can now run daily.py - it will upload automatically.")


if __name__ == "__main__":
    main()
