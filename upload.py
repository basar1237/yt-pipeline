#!/usr/bin/env python3
"""
YouTube Shorts uploader. Uses an OAuth refresh token saved at
credentials/token.json (created once by oauth_setup.py).

CLI:
    python3 upload.py PATH "TITLE" "DESCRIPTION" [public|unlisted|private]
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CRED_DIR = ROOT / "credentials"
CLIENT_SECRETS = CRED_DIR / "client_secrets.json"
TOKEN_FILE = CRED_DIR / "token.json"

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def get_credentials():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    if not TOKEN_FILE.exists():
        raise SystemExit(
            "credentials/token.json missing. Run oauth_setup.py once to create it."
        )
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return creds


def upload_short(video_path, title, description, tags=None, privacy="public"):
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    if tags is None:
        tags = ["shorts", "ilginc", "bilgi"]

    creds = get_credentials()
    youtube = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "tags": tags,
            "categoryId": "22",  # People & Blogs (works for shorts)
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
    req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = req.next_chunk()
        if status:
            print(f"  upload progress: {int(status.progress() * 100)}%", flush=True)
    return response["id"]


def main():
    if len(sys.argv) < 4:
        print("Usage: upload.py VIDEO_PATH TITLE DESCRIPTION [privacy]")
        sys.exit(1)
    video, title, desc = sys.argv[1], sys.argv[2], sys.argv[3]
    privacy = sys.argv[4] if len(sys.argv) > 4 else "public"
    vid = upload_short(video, title, desc, privacy=privacy)
    print(f"https://youtube.com/shorts/{vid}")


if __name__ == "__main__":
    main()
