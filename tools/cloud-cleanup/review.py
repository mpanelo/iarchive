"""Loopback selection editor. There is deliberately no execution HTTP endpoint."""

from contextlib import contextmanager
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading

from prototype import Blocked


ASSETS = Path(__file__).parent
MAX_BODY = 16 * 1024
CSP = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


def cards(data: dict) -> str:
    if data.get('review_draft') is True:
        return draft_cards(data)
    selected = {a["id"] for a in data["targets"]}
    result = []
    names = {"photo": "Photo", "live": "Live Photo", "raw_pair": "RAW + JPEG"}
    for index, asset in enumerate(data["snapshot"]["active"], 1):
        checked = asset["id"] in selected
        resources = "".join(f"<li><span>{escape(r['role'].replace('_', ' '))}</span>"
                            f"<span>{r['size']} B</span></li>" for r in asset["resources"])
        result.append(f"""<article class="asset-card {'selected' if checked else 'kept'}">
<div class="illustration art-{index % 3}" role="img" aria-label="Synthetic illustration {index}">
<span class="illustration-label">SYNTHETIC PREVIEW</span><span class="illustration-number">0{index}</span></div>
<div class="card-body"><div class="card-title"><h3>Test item {index}</h3>
<span class="kind">{names.get(asset['kind'], escape(asset['kind']))}</span></div>
<p class="asset-id">{escape(asset['id'])}</p>
<details><summary>{len(asset['resources'])} archived variants</summary><ul class="variants">{resources}</ul></details>
<label class="selection"><input type="checkbox" value="{escape(asset['id'], quote=True)}"
{'checked' if checked else ''} aria-label="Remove test item {index} from cloud">
<span class="selection-label">{'Remove from cloud' if checked else 'Keep in cloud'}</span></label>
<p class="selection-note">All variants stay together. Local copies are kept.</p></div></article>""")
    return "".join(result)


def draft_cards(data):
    selected = {a['id'] for a in data['targets']}
    result = []
    for asset in data['snapshot']['active']:
        checked = asset['id'] in selected
        original = next(r for r in asset['resources'] if r['resource_field'] == 'resOriginalRes')
        title = original['relative_path'].rsplit('/', 1)[-1]
        resources = ''.join(f"<li><span>{escape(r['role'].replace('_', ' '))}"
                            f"<code>{escape(r['relative_path'])}</code></span><span>{r['size']:,} B</span></li>"
                            for r in asset['resources'])
        result.append(f'''<article class="asset-card {'selected' if checked else 'kept'}">
<div class="card-body"><h3>{escape(title)}</h3><p class="asset-id">{escape(asset['id'])}</p>
<p class="helper">Resource paths only · media previews are not generated.</p>
<details open><summary>{len(asset['resources'])} observed variants</summary><ul class="variants">{resources}</ul></details>
<label class="selection"><input type="checkbox" value="{escape(asset['id'], quote=True)}"
{'checked' if checked else ''} aria-label="Include {escape(title, quote=True)} in review draft">
<span class="selection-label">{'Include in draft' if checked else 'Keep in iCloud'}</span></label>
<p class="selection-note">All variants stay together. Saving does not remove anything.</p></div></article>''')
    return ''.join(result)


def render(data: dict | None = None, phrase: str = "", *, draft_mode=False) -> str:
    draft_mode = draft_mode or (data is not None and data.get('review_draft') is True)
    html = (ASSETS / "review.html").read_text(encoding="utf-8")
    if draft_mode:
        replacements = {
            'OFFLINE DEMO': 'REVIEW DRAFT', 'Choose what to clear.': 'Choose items for your draft.',
            'Uncheck anything you want to keep in iCloud.': 'Keep items unchecked to leave them out of your draft.',
            'Synthetic items only. Previews are illustrations, not decoded media. Nothing will be removed from iCloud.':
                'Local evidence only. Saving a draft does not authorize removal or change iCloud.',
            'Confirm in Terminal': 'Draft saved', 'Demo limits': 'Verification limits',
            'Both folders are on one filesystem. Independent physical drives, real cloud relationships, full coverage, recovery and reclaimed space remain NOT VERIFIED.':
                'Local bytes are rechecked when saving. Current physical-drive independence, cloud coverage and complete resource relationships remain NOT VERIFIED.',
            'Keep items unchanged during cleanup. A preflight read cannot prevent a change before removal. This mock deliberately has no atomic version guard.':
                'These are review notes. A future cloud action would require new checks and separate approval.',
            'Confirm your saved selection.': 'Your draft is saved.',
            'Copy this confirmation into the waiting Terminal. This runs the simulation only.':
                'You can adjust your selection and save a new draft, or close this page. No Terminal confirmation is needed.',
            'Current saved confirmation': 'Saved draft digest', 'Copy confirmation': 'Copy draft digest',
            'Save selection': 'Save draft',
        }
        for old, new in replacements.items():
            html = html.replace(old, new)
        status = '' if data is None else ''.join(f'<dt>{escape(key)}</dt><dd>{escape(value)}</dd>'
                                                 for key, value in data['verification'].items())
        notice = '' if data is None else '<p>Saved review draft · read-only. This is not removal approval.</p>'
        html = html.replace('__NOTICE__', notice + f'<dl id="verification-status" class="notice">{status}</dl>')
    if data is None:
        return html.replace("__CONTENT__", "").replace("__BACKUPS__", "").replace("__NOTICE__", "")
    # Retained snapshots are read-only. Only the loopback editor can save choices.
    css = (ASSETS / "review.css").read_text(encoding="utf-8")
    html = html.replace('<link rel="stylesheet" href="/review.css">', f"<style>{css}</style>")
    html = html.replace('<script src="/review.js" defer></script>', "")
    html = html.replace("<head>", "<head><meta http-equiv=\"Content-Security-Policy\" "
                        "content=\"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">")
    html = html.replace("__CONTENT__", cards(data)).replace("__BACKUPS__", "".join(
        f"<li><strong>Copy {n}</strong><code>{escape(root)}</code></li>" for n, root in enumerate(data["roots"], 1)))
    html = html.replace("__NOTICE__", "<p class=\"notice\">Saved preview · read-only. "
                        "Run the demo with --simulate to edit the selection.</p>")
    html = html.replace('id="selection-summary">Loading review…',
                        f'id="selection-summary">{data["count"]} selected · {len(data["snapshot"]["active"]) - data["count"]} kept')
    html = html.replace('id="confirmation" readonly', f'id="confirmation" value="{escape(phrase, quote=True)}" readonly')
    if draft_mode:
        html = html.replace('id="next-step" class="next-step" hidden', 'id="next-step" class="next-step"')
        html = html.replace('id="save" class="primary" type="button" disabled>Save draft',
                            'id="save" class="primary" type="button" disabled>Draft saved')
        html = html.replace('id="save-status" class="helper">Connect using the link printed in Terminal.',
                            'id="save-status" class="helper">Close this page or reopen the local review to make changes.')
    # Nothing in a static snapshot should look actionable.
    html = html.replace('type="checkbox"', 'type="checkbox" disabled')
    return html


def parse_body(raw: str) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate key")
            result[key] = value
        return result

    def invalid(_value):
        raise ValueError("Nonfinite value")

    body = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)
    if not isinstance(body, dict):
        raise ValueError("Expected an object")
    return body


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *_args):
        pass  # Never log bearer tokens or posted selections.

    def reply(self, status: int, body, content_type="application/json"):
        if content_type == "application/json":
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        for key, value in (("Content-Type", content_type), ("Content-Length", str(len(body))),
                           ("Cache-Control", "no-store"), ("Content-Security-Policy", CSP),
                           ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer")):
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def authorized(self, *, api=False, mutation=False) -> bool:
        expected_origin = "http://" + self.server.authority
        if self.headers.get_all("Host") != [self.server.authority]:
            return False
        origins = self.headers.get_all("Origin", [])
        if origins and origins != [expected_origin]:
            return False
        if mutation and origins != [expected_origin]:
            return False
        if api:
            auth = self.headers.get_all("Authorization", [])
            if len(auth) != 1 or not secrets.compare_digest(auth[0].encode(), ("Bearer " + self.server.token).encode()):
                return False
        return True

    def do_GET(self):
        if not self.authorized(api=self.path.startswith("/api/")):
            return self.reply(403, {"error": "Open the complete review link printed in Terminal"})
        if self.path == "/":
            return self.reply(200, render(draft_mode=getattr(self.server.session, 'review_draft', False)), "text/html; charset=utf-8")
        if self.path in ("/review.css", "/review.js"):
            mime = "text/css" if self.path.endswith(".css") else "text/javascript"
            return self.reply(200, (ASSETS / self.path[1:]).read_bytes(), mime + "; charset=utf-8")
        if self.path == "/api/state":
            return self.reply(200, self.server.session.state())
        self.reply(404, {"error": "Unknown review endpoint"})

    def do_POST(self):
        if not self.authorized(api=True, mutation=True):
            return self.reply(403, {"error": "Review authorization or origin did not match"})
        if self.path not in ("/api/draft", "/api/save"):
            return self.reply(404, {"error": "Unknown review endpoint"})
        lengths = self.headers.get_all("Content-Length", [])
        if (self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1
                or len(lengths[0]) > 6 or not lengths[0].isascii() or not lengths[0].isdigit()
                or not 0 < int(lengths[0]) <= MAX_BODY
                or self.headers.get_all("Content-Type") != ["application/json"]):
            return self.reply(400, {"error": "Expected a bounded JSON selection"})
        try:
            raw = self.rfile.read(int(lengths[0]))
            if len(raw) != int(lengths[0]):
                raise ValueError("Incomplete selection body")
            body = parse_body(raw.decode("utf-8"))
            required = {"revision", "selected"} if self.path == "/api/draft" else {"revision"}
            if set(body) != required or type(body["revision"]) is not int:
                raise ValueError("Unexpected selection fields")
            if self.path == "/api/draft":
                state = self.server.session.draft(body["revision"], body["selected"])
            else:
                state = self.server.session.save(body["revision"])
            self.reply(200, state)
        except (ValueError, OSError):
            self.reply(400, {"error": "Could not read a valid bounded selection"})
        except Blocked as error:
            self.reply(409, {"error": str(error)})


@contextmanager
def serve(session):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.authority = f"127.0.0.1:{server.server_port}"
    server.token = secrets.token_hex(32)
    server.session = session
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{server.authority}/#{server.token}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
