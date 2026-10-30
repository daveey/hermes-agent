#!/usr/bin/env python3
"""Local helper for the GitHub App manifest flow (David's click).
GET /          -> auto-submitting form that POSTs github-app-manifest.json to github.com/settings/apps/new
GET /callback  -> runs store-github-app.sh <code> (credentials go straight to SSM), then redirects to the install page.
Nothing secret is printed or written to disk."""
import html, http.server, json, pathlib, secrets, subprocess, sys, urllib.parse
HERE = pathlib.Path(__file__).resolve().parent
MANIFEST = (HERE / 'github-app-manifest.json').read_text()
STATE = secrets.token_urlsafe(16)
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query)
        if u.path == '/':
            body = f'''<form id=f method=post action="https://github.com/settings/apps/new?state={STATE}">
<input type=hidden name=manifest value="{html.escape(MANIFEST)}"><button>Continue to GitHub</button></form>
<script>document.getElementById('f').submit()</script>'''
            return self.reply(200, body)
        if u.path == '/callback':
            if q.get('state', [''])[0] != STATE or 'code' not in q:
                return self.reply(400, 'bad state or missing code')
            r = subprocess.run([str(HERE / 'store-github-app.sh'), q['code'][0]], capture_output=True, text=True, check=False, timeout=120)
            print(r.stdout.strip(), r.stderr.strip()[-300:], 'rc', r.returncode, flush=True)
            if r.returncode != 0:
                return self.reply(500, 'storing the app failed; see the session log')
            slug = r.stdout.strip().rsplit('(', 1)[-1].rstrip(')')
            self.send_response(302); self.send_header('Location', f'https://github.com/apps/{slug}/installations/new'); self.end_headers()
            print('DONE', flush=True); return
        self.reply(404, 'not found')
    def reply(self, code, body):
        self.send_response(code); self.send_header('Content-Type', 'text/html'); self.end_headers(); self.wfile.write(body.encode())
    def log_message(self, *a): pass
http.server.HTTPServer(('127.0.0.1', 8765), H).serve_forever()
