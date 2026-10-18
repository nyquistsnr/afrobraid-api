# uvicorn app.main:app --reload

# arq app.worker.WorkerSettings

# alembic upgrade head

# python -m scripts.seed_styles

# python -m scripts.seed_braiders               (1,000 braiders across DE/FR/GB; --count N for more)

# python -m scripts.link_seed_braiders_stripe   (real, auto-verified Stripe TEST Connect accounts for the seeded braiders)

# python -m scripts.prune_seed_braiders --keep 1000   (--keep N and/or --linked-only)

# 1. Google — easiest, works in a browser right now

Google issues an ID token (a signed JWT) straight to client-side JavaScript after sign-in — no server-side secret needed for this step. I built you a small local test page for exactly this: dev-tools/social-login-test/google.html. It's already open in the Browser pane.

Steps:

Register the origin you'll serve this page from. In Google Cloud Console, open the OAuth Client whose ID is already in your .env (1098574715382-...), and add http://localhost:5500 under Authorized JavaScript origins.
Serve the page (Google's SDK refuses to run on a bare file:// page — it needs a real origin):
cd dev-tools/social-login-test && python -m http.server 5500
Restart your API so it picks up the CORS change I just made (I added http://localhost:5500 to CORS_ORIGINS in .env):
uvicorn app.main:app --reload
Open http://localhost:5500/google.html in your real browser (not the embedded one — this needs your actual Google login).
Paste your Client ID (1098574715382-lnmfivbu7l9tu0nldlrb4rdk50rqd73t.apps.googleusercontent.com) into the first field, click "Load Google Sign-In button", then actually sign in with a real Google account.
The ID token appears in the textarea — that's your provider_token. Click "Call /api/v1/auth/social/google" and you'll see the real API response right there on the page (tokens on first signup, or a login on repeat).

2. TikTok — needs a real HTTPS redirect, so it's a curl-based flow
   TikTok's docs are explicit: redirect URIs must be https:// — no localhost. So you can't just open a local HTML file. You already have ngrok installed, which is the natural fix: it gives you a temporary public HTTPS URL that tunnels to your machine.

Step A — get an HTTPS redirect URI:

ngrok http 8000
This prints something like https://abcd1234.ngrok-free.app. In the TikTok Developer Portal, add https://abcd1234.ngrok-free.app/callback as a registered redirect URI on your app (Login Kit product).

Step B — send yourself through TikTok's real consent screen. Build this URL (I've filled in your real client key) and open it in your real browser:

https://www.tiktok.com/v2/auth/authorize/?client_key=sbawp7u8iwkt6yr23w&response_type=code&scope=user.info.basic,user.info.profile,user.info.email&redirect_uri=https://abcd1234.ngrok-free.app/callback&state=teststate123
(swap in your actual ngrok URL). Log in with a real TikTok account and approve.

Step C — grab the code. After you approve, the browser redirects to your ngrok URL — even if nothing is running there, the address bar will show ...?code=SOME_LONG_CODE&state=teststate123. Copy that code immediately (these expire in minutes).

Step D — exchange the code for an access token (this needs your client secret, so it stays in a terminal, never in browser JS):

curl -X POST https://open.tiktokapis.com/v2/oauth/token/ \
 -H "Content-Type: application/x-www-form-urlencoded" \
 -d "client_key=sbawp7u8iwkt6yr23w" \
 -d "client_secret=HXW0vPJUmZcnkyvx3hfZvB60tPprv50m" \
 -d "code=PASTE_YOUR_CODE_HERE" \
 -d "grant_type=authorization_code" \
 -d "redirect_uri=https://abcd1234.ngrok-free.app/callback"
This returns JSON with an access_token — that's your provider_token for TikTok.

Step E — test our endpoint:

curl -X POST http://localhost:8000/api/v1/auth/social/tiktok \
 -H "Content-Type: application/json" \
 -d '{"provider_token":"PASTE_ACCESS_TOKEN_HERE","user_type":"CUSTOMER"}'
⚠️ Heads up on TikTok specifically: unless your app has been explicitly approved for the email scope, TikTok's /v2/user/info/ endpoint won't return an email address at all — even if you requested it in scope. Our code (tiktok.py) requires an email (matching your schema), so you'll likely get back SOCIAL_AUTH_FAILED even with a perfectly valid token. That's expected, correct behavior on our side, not a bug — it just means TikTok sign-up can't fully work until that scope is approved for your app.
