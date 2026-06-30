"""
One-time helper to obtain a Gmail OAuth refresh token. Run LOCALLY, not in CI.

Setup:
  1. Google Cloud Console → APIs & Services → enable "Gmail API".
  2. OAuth consent screen → User type "Internal" (so the token doesn't expire in
     7 days) → add scope .../auth/gmail.readonly.
  3. Credentials → Create OAuth client ID → type "Desktop app" → download JSON,
     save it next to this script as `client_secret.json`.
  4. pip install google-auth-oauthlib
  5. python get_token.py
     → a browser opens. Log in AS THE INQUIRIES MAILBOX and approve.
     → the script prints GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN.
  6. Add those three as GitHub repository secrets.
"""

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

print("\n" + "=" * 60)
print("Add these as GitHub repository secrets:\n")
print("GMAIL_CLIENT_ID     =", creds.client_id)
print("GMAIL_CLIENT_SECRET =", creds.client_secret)
print("GMAIL_REFRESH_TOKEN =", creds.refresh_token)
print("=" * 60)
if not creds.refresh_token:
    print("\n⚠️ No refresh token returned. Revoke prior access at "
          "https://myaccount.google.com/permissions and re-run.")
