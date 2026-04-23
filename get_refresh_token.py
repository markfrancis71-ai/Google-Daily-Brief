"""
Run this once locally to get your Google OAuth refresh token.
You'll need your Client ID and Client Secret from Google Cloud Console.
"""
import json
import webbrowser
import urllib.parse
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler

CLIENT_ID = input("Enter your Google Client ID: ").strip()
CLIENT_SECRET = input("Enter your Google Client Secret: ").strip()

SCOPES = " ".join([
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/tasks.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
])

REDIRECT_URI = "http://localhost:8080"
auth_code = None


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        auth_code = params.get("code", [None])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<h2>Authentication successful! You can close this tab.</h2>")

    def log_message(self, format, *args):
        pass  # suppress server logs


auth_url = (
    "https://accounts.google.com/o/oauth2/auth"
    f"?client_id={urllib.parse.quote(CLIENT_ID)}"
    f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
    f"&response_type=code"
    f"&scope={urllib.parse.quote(SCOPES)}"
    f"&access_type=offline"
    f"&prompt=consent"
)

print("\nOpening browser for Google login...")
webbrowser.open(auth_url)
print("Waiting for authentication...")

server = HTTPServer(("localhost", 8080), CallbackHandler)
server.handle_request()

if not auth_code:
    print("ERROR: No auth code received.")
    exit(1)

data = urllib.parse.urlencode({
    "code": auth_code,
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "redirect_uri": REDIRECT_URI,
    "grant_type": "authorization_code",
}).encode()

req = urllib.request.Request(
    "https://oauth2.googleapis.com/token",
    data=data,
    headers={"Content-Type": "application/x-www-form-urlencoded"},
)
response = json.loads(urllib.request.urlopen(req).read())

refresh_token = response.get("refresh_token")
if refresh_token:
    print("\n" + "="*60)
    print("SUCCESS! Add these to your GitHub repo secrets:")
    print("="*60)
    print(f"GOOGLE_CLIENT_ID:     {CLIENT_ID}")
    print(f"GOOGLE_CLIENT_SECRET: {CLIENT_SECRET}")
    print(f"GOOGLE_REFRESH_TOKEN: {refresh_token}")
    print("="*60)
else:
    print("ERROR: No refresh token in response.")
    print(json.dumps(response, indent=2))
