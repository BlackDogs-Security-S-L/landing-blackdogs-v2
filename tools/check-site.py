#!/usr/bin/env python3
"""Static checks for the BlackDogs site: no build step, no dependencies.

Run: python3 tools/check-site.py
"""
import html.parser
import pathlib
import re
import sys
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = "https://blackdogs.io"
ALLOWED_REMOTE_SCHEMES = {"https", "mailto", "tel"}

errors = []


def fail(where, msg):
    errors.append(f"{where}: {msg}")


class Page(html.parser.HTMLParser):
    """Collects the bits we assert on, and the tag stack for well-formedness."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.unbalanced = []
        self.links = []        # (attr_name, value)
        self.ids = set()
        self.meta = {}         # name/property -> content
        self.canonical = None
        self.alternates = {}   # hreflang -> href
        self.title = None
        self.lang = None
        self.headings = []     # (level, text)
        self.imgs_without_alt = 0
        self._in_title = False
        self._heading = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag not in self.VOID:
            self.stack.append(tag)
        if "id" in a:
            self.ids.add(a["id"])
        if tag == "html":
            self.lang = a.get("lang")
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = a.get("name") or a.get("property")
            if key:
                self.meta[key] = a.get("content", "")
        elif tag == "link":
            rel = a.get("rel", "")
            if rel == "canonical":
                self.canonical = a.get("href")
            elif rel == "alternate" and "hreflang" in a:
                self.alternates[a["hreflang"]] = a.get("href")
            if "href" in a:
                self.links.append(("href", a["href"]))
        elif tag == "img":
            if not a.get("alt") and "alt" not in a:
                self.imgs_without_alt += 1
            if "src" in a:
                self.links.append(("src", a["src"]))
        elif tag in ("script", "a", "source", "iframe"):
            for k in ("src", "href"):
                if k in a:
                    self.links.append((k, a[k]))
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._heading = [int(tag[1]), ""]

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.unbalanced.append((tag, self.getpos()[0]))
            if tag in self.stack:                      # resync
                while self.stack and self.stack.pop() != tag:
                    pass
            return
        self.stack.pop()
        if tag == "title":
            self._in_title = False
        elif tag.startswith("h") and self._heading:
            self.headings.append(tuple(self._heading))
            self._heading = None

    def handle_data(self, data):
        if self._in_title:
            self.title = (self.title or "") + data
        if self._heading is not None:
            self._heading[1] += data


def parse(path):
    p = Page()
    p.feed(path.read_text(encoding="utf-8"))
    p.close()
    return p


def check_links(path, page):
    """Every relative href/src must resolve to a file that exists."""
    for attr, raw in page.links:
        url = urllib.parse.urlsplit(raw)
        if url.scheme:
            if url.scheme not in ALLOWED_REMOTE_SCHEMES:
                fail(path, f"non-https external {attr}: {raw}")
            continue
        if not url.path:                                # pure fragment
            if url.fragment and url.fragment not in page.ids:
                fail(path, f"fragment #{url.fragment} has no matching id")
            continue
        if raw.startswith("//"):
            fail(path, f"protocol-relative {attr}: {raw}")
            continue
        target = (path.parent / url.path).resolve()
        if not target.exists():
            fail(path, f"broken {attr} -> {raw}")
        if url.fragment and target.suffix == ".html" and target.exists():
            if url.fragment not in parse(target).ids:
                fail(path, f"broken fragment -> {raw}")


def check_page(path, expect_canonical=None, expect_lang=None):
    page = parse(path)
    rel = path.relative_to(ROOT)

    if page.unbalanced:
        fail(rel, f"unbalanced tags: {page.unbalanced}")
    if page.stack:
        fail(rel, f"unclosed tags: {page.stack}")
    if not page.title:
        fail(rel, "missing <title>")
    if not page.meta.get("description"):
        fail(rel, "missing meta description")
    if not page.meta.get("viewport"):
        fail(rel, "missing viewport meta (responsive)")
    for prop in ("og:title", "og:description", "og:url", "og:image", "og:type"):
        if not page.meta.get(prop):
            fail(rel, f"missing {prop}")
    if expect_lang and page.lang != expect_lang:
        fail(rel, f"lang is {page.lang!r}, expected {expect_lang!r}")
    if expect_canonical and page.canonical != expect_canonical:
        fail(rel, f"canonical is {page.canonical!r}, expected {expect_canonical!r}")
    if page.imgs_without_alt:
        fail(rel, f"{page.imgs_without_alt} <img> without alt")

    h1s = [t for lvl, t in page.headings if lvl == 1]
    if len(h1s) != 1:
        fail(rel, f"expected exactly one <h1>, found {len(h1s)}")
    prev = 0
    for lvl, text in page.headings:
        if prev and lvl > prev + 1:
            fail(rel, f"heading jumps h{prev} -> h{lvl} ({text.strip()[:40]!r})")
        prev = lvl

    check_links(path, page)
    return page


def check_hreflang(pages):
    """Each language set must cross-reference every sibling plus x-default."""
    for group in pages:
        hrefs = {lang: url for lang, url, _ in group}
        for lang, url, path in group:
            page = parse(ROOT / path)
            for other, other_url in hrefs.items():
                if page.alternates.get(other) != other_url:
                    fail(path, f"hreflang {other} is {page.alternates.get(other)!r}, expected {other_url!r}")
            if page.alternates.get("x-default") != hrefs["en"]:
                fail(path, "x-default must point at the English page")


def check_security_txt():
    path = ROOT / ".well-known/security.txt"
    if not path.exists():
        return fail(".well-known/security.txt", "missing")
    text = path.read_text(encoding="utf-8")
    fields = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        if ":" not in line:
            fail("security.txt", f"malformed line: {line!r}")
            continue
        k, v = line.split(":", 1)
        fields.setdefault(k.strip(), []).append(v.strip())
    for required in ("Contact", "Expires"):            # RFC 9116 §2.5.x
        if required not in fields:
            fail("security.txt", f"missing required field {required}")
    if len(fields.get("Expires", [])) > 1:
        fail("security.txt", "Expires must appear exactly once")
    if len(fields.get("Canonical", [])) > 1 and BASE not in fields["Canonical"][0]:
        fail("security.txt", "Canonical does not point at this site")
    for url in fields.get("Policy", []):
        if "vulnerability-disclosure" not in url:
            fail("security.txt", f"Policy should point at the CVD policy, got {url}")
    for url in fields.get("Encryption", []):
        local = ROOT / url.replace(BASE + "/", "")
        if not local.exists():
            fail("security.txt", f"Encryption key not published: {url}")
    if (ROOT / ".nojekyll").exists() is False:
        fail(".nojekyll", "missing — GitHub Pages will not serve /.well-known/")


def check_pgp_key():
    key = ROOT / "csirt/csirt-blackdogs.asc"
    if not key.exists():
        return fail("csirt/csirt-blackdogs.asc", "missing")
    text = key.read_text(encoding="utf-8")
    if "BEGIN PGP PUBLIC KEY BLOCK" not in text:
        fail("csirt-blackdogs.asc", "not an armored PGP public key")
    fp = "05D9A242CE24E6D013F2A060881C5CE5806EE9B3"
    spaced = " ".join(fp[i:i + 4] for i in range(0, len(fp), 4))
    for page in ROOT.glob("csirt/**/index.html"):
        body = page.read_text(encoding="utf-8")
        if "Fingerprint" in body or "Huella" in body or "Empremta" in body or "fingerprint" in body:
            normalised = re.sub(r"(&nbsp;|\s)+", " ", body)
            if spaced.replace("  ", " ") not in normalised:
                fail(page.relative_to(ROOT), "published PGP fingerprint does not match the key")


def check_sitemap():
    import xml.etree.ElementTree as ET
    tree = ET.parse(ROOT / "sitemap.xml")
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = {e.text for e in tree.getroot().findall(".//s:loc", ns)}
    for expected in (f"{BASE}/csirt", f"{BASE}/csirt/es", f"{BASE}/csirt/ca",
                     f"{BASE}/csirt/rfc2350", f"{BASE}/csirt/rfc2350/es",
                     f"{BASE}/csirt/rfc2350/ca",
                     f"{BASE}/csirt/vulnerability-disclosure",
                     f"{BASE}/csirt/vulnerability-disclosure/es",
                     f"{BASE}/csirt/vulnerability-disclosure/ca"):
        if expected not in locs:
            fail("sitemap.xml", f"missing {expected}")


def check_no_inline_handlers():
    """No inline event handlers or javascript: URLs anywhere (XSS hygiene)."""
    for path in ROOT.rglob("*.html"):
        if ".git" in path.parts:
            continue
        body = path.read_text(encoding="utf-8")
        for m in re.finditer(r"\son[a-z]+\s*=", body):
            fail(path.relative_to(ROOT), f"inline event handler at offset {m.start()}")
        if "javascript:" in body:
            fail(path.relative_to(ROOT), "javascript: URL")


RFC_PAGES = ["csirt/rfc2350/index.html", "csirt/rfc2350/es/index.html",
             "csirt/rfc2350/ca/index.html"]
CSIRT_PAGES = ["csirt/index.html", "csirt/es/index.html", "csirt/ca/index.html"]


def check_rfc_structure():
    """Sections added after review must not be silently dropped by a later edit."""
    for rel in RFC_PAGES:
        body = (ROOT / rel).read_text(encoding="utf-8")
        for marker in ("3.5", "4.4"):
            if f">{marker} " not in body:
                fail(rel, f"RFC 2350 section {marker} is missing")
        for n in range(1, 8):
            if f'id="s{n}"' not in body:
                fail(rel, f"RFC 2350 top-level section {n} is missing")


def check_retired_wording():
    """Policies we deliberately walked back must not reappear anywhere."""
    retired = [
        ("TLP:AMBER by default", "auto-AMBER default creates an unmet operational obligation"),
        ("TLP:AMBER por defecto", "auto-AMBER default (es)"),
        ("com a TLP:AMBER", "auto-AMBER default (ca)"),
        ("must be encrypted with the PGP key", "PGP 'must' forbids agreed alternative channels"),
        ("with presence in Andorra", "superseded by the established-entity wording"),
        ("presencia en Andorra", "superseded by the established-entity wording (es)"),
        ("presència a Andorra", "superseded by the established-entity wording (ca)"),
        ("or related cybersecurity services", "constituency scope too broad"),
        ("To be published", "operating hours are defined now"),
        ("Pendiente de publicar", "operating hours are defined now (es)"),
        ("Pendent de publicar", "operating hours are defined now (ca)"),
        ("not published as generic service levels", "response targets are published now"),
        ("no se publican como niveles de servicio", "response targets are published now (es)"),
        ("no es publiquen com a nivells de servei", "response targets are published now (ca)"),
    ]
    for path in ROOT.glob("csirt/**/index.html"):
        body = path.read_text(encoding="utf-8")
        for phrase, why in retired:
            if phrase in body:
                fail(path.relative_to(ROOT), f"retired wording present ({why}): {phrase!r}")


def check_page_rfc_consistency():
    """The /csirt pages must carry the same constituency and authority limits as the RFC."""
    required = {
        "csirt/index.html": ["not automatically considered constituents",
                             "may range from advisory and coordination-only functions",
                             "Post-Incident Review"],
        "csirt/es/index.html": ["no se consideran automáticamente constituents",
                                "exclusivamente de asesoramiento y coordinación",
                                "Revisión posterior al incidente"],
        "csirt/ca/index.html": ["no es consideren automàticament constituents",
                                "exclusivament d'assessorament i coordinació",
                                "Revisió posterior a l'incident"],
    }
    for rel, phrases in required.items():
        body = (ROOT / rel).read_text(encoding="utf-8")
        for phrase in phrases:
            if phrase not in body:
                fail(rel, f"missing wording that must mirror the RFC: {phrase!r}")



PAGES = [
    ("index.html", f"{BASE}/", "es"),
    ("ca/index.html", f"{BASE}/ca/", "ca"),
    ("en/index.html", f"{BASE}/en/", "en"),
    ("csirt/index.html", f"{BASE}/csirt", "en"),
    ("csirt/es/index.html", f"{BASE}/csirt/es", "es"),
    ("csirt/ca/index.html", f"{BASE}/csirt/ca", "ca"),
    ("csirt/rfc2350/index.html", f"{BASE}/csirt/rfc2350", "en"),
    ("csirt/rfc2350/es/index.html", f"{BASE}/csirt/rfc2350/es", "es"),
    ("csirt/rfc2350/ca/index.html", f"{BASE}/csirt/rfc2350/ca", "ca"),
    ("csirt/vulnerability-disclosure/index.html", f"{BASE}/csirt/vulnerability-disclosure", "en"),
    ("csirt/vulnerability-disclosure/es/index.html", f"{BASE}/csirt/vulnerability-disclosure/es", "es"),
    ("csirt/vulnerability-disclosure/ca/index.html", f"{BASE}/csirt/vulnerability-disclosure/ca", "ca"),
    ("legal/index.html", f"{BASE}/legal", "en"),
    ("legal/es/index.html", f"{BASE}/legal/es", "es"),
    ("legal/ca/index.html", f"{BASE}/legal/ca", "ca"),
    ("privacy/index.html", f"{BASE}/privacy", "en"),
    ("privacy/es/index.html", f"{BASE}/privacy/es", "es"),
    ("privacy/ca/index.html", f"{BASE}/privacy/ca", "ca"),
]

CSIRT_NAV = {
    "index.html": "csirt/index.html",
    "ca/index.html": "../csirt/ca/index.html",
    "en/index.html": "../csirt/index.html",
}


EXPECTED_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
                "base-uri 'none'; form-action 'none'; upgrade-insecure-requests")


def check_csp():
    """The meta CSP is the only header-like control GitHub Pages allows us to ship.

    It only holds while the pages stay free of inline styles/scripts and external
    origins, so this checks the policy and the things that would force it open.
    """
    for rel, _, _ in PAGES:
        path = ROOT / rel
        if not path.exists():
            continue
        body = path.read_text(encoding="utf-8")

        page = parse(path)
        csp = None
        for meta in re.finditer(r'<meta http-equiv="Content-Security-Policy" content="([^"]*)"', body):
            csp = meta.group(1)
        if csp is None:
            fail(rel, "no meta Content-Security-Policy")
        elif csp != EXPECTED_CSP:
            fail(rel, f"CSP differs from the reviewed policy:\n      got      {csp}\n      expected {EXPECTED_CSP}")

        if page.meta.get("referrer") != "strict-origin-when-cross-origin":
            fail(rel, f"referrer meta is {page.meta.get('referrer')!r}, expected 'strict-origin-when-cross-origin'")

        # things that would silently require loosening the policy
        if re.search(r'\sstyle="', body):
            fail(rel, "inline style= attribute — would need style-src 'unsafe-inline'")
        if re.search(r'<style\b', body):
            fail(rel, "inline <style> block — would need style-src 'unsafe-inline'")
        for m in re.finditer(r'<script\b([^>]*)>', body):
            attrs = m.group(1)
            if "src=" not in attrs and "ld+json" not in attrs:
                fail(rel, "inline <script> — would need script-src 'unsafe-inline'")
        for m in re.finditer(r'(?:src|href)="(https?://[^"]+)"', body):
            url = m.group(1)
            if not url.startswith(BASE):
                fail(rel, f"off-origin resource/link would be blocked by the CSP: {url}")



SEVERITY_TARGETS = {
    "csirt/index.html":    ["4 hours", "1 business day", "2 business days", "5 business days"],
    "csirt/es/index.html": ["4 horas", "1 día hábil", "2 días hábiles", "5 días hábiles"],
    "csirt/ca/index.html": ["4 hores", "1 dia hàbil", "2 dies hàbils", "5 dies hàbils"],
}
RFC_TARGETS = {
    "csirt/rfc2350/index.html":    "Critical — 4 hours; High — 1 business day; Medium — 2 business days; Low — 5 business days",
    "csirt/rfc2350/es/index.html": "Critical — 4 horas; High — 1 día hábil; Medium — 2 días hábiles; Low — 5 días hábiles",
    "csirt/rfc2350/ca/index.html": "Critical — 4 hores; High — 1 dia hàbil; Medium — 2 dies hàbils; Low — 5 dies hàbils",
}


def check_service_commitments():
    """Operating hours and response targets are public promises: they must be
    present, identical in substance across languages, and match the RFC."""
    for rel, targets in SEVERITY_TARGETS.items():
        body = (ROOT / rel).read_text(encoding="utf-8")
        if "09:00" not in body or "18:00" not in body:
            fail(rel, "operating hours missing from the contact card")
        order = [body.find(f">{t}</p>") for t in targets]
        if -1 in order:
            fail(rel, f"severity response targets missing: {targets}")
        elif order != sorted(order):
            fail(rel, "severity targets are out of Critical/High/Medium/Low order")
        if body.count('class="sev-target"') != 4:
            fail(rel, f'expected 4 sev-target lines, found {body.count(chr(34)+"sev-target"+chr(34))}')

    for rel, line in RFC_TARGETS.items():
        body = (ROOT / rel).read_text(encoding="utf-8")
        if line not in body:
            fail(rel, f"RFC 2350 §4.1 response targets missing or altered:\n      expected {line}")
        if "09:00" not in body or "18:00" not in body:
            fail(rel, "RFC 2350 §2.11 operating hours missing")
        if "BlackDogs Security Andorra S.L." not in body:
            fail(rel, "RFC 2350 §3.3 Andorran entity name missing")



def main():
    for rel, canonical, lang in PAGES:
        path = ROOT / rel
        if not path.exists():
            fail(rel, "page missing")
            continue
        check_page(path, canonical, lang)

    check_hreflang([
        [("en", f"{BASE}/csirt", "csirt/index.html"),
         ("es", f"{BASE}/csirt/es", "csirt/es/index.html"),
         ("ca", f"{BASE}/csirt/ca", "csirt/ca/index.html")],
        [("en", f"{BASE}/csirt/rfc2350", "csirt/rfc2350/index.html"),
         ("es", f"{BASE}/csirt/rfc2350/es", "csirt/rfc2350/es/index.html"),
         ("ca", f"{BASE}/csirt/rfc2350/ca", "csirt/rfc2350/ca/index.html")],
        [("en", f"{BASE}/csirt/vulnerability-disclosure", "csirt/vulnerability-disclosure/index.html"),
         ("es", f"{BASE}/csirt/vulnerability-disclosure/es", "csirt/vulnerability-disclosure/es/index.html"),
         ("ca", f"{BASE}/csirt/vulnerability-disclosure/ca", "csirt/vulnerability-disclosure/ca/index.html")],
        [("en", f"{BASE}/legal", "legal/index.html"),
         ("es", f"{BASE}/legal/es", "legal/es/index.html"),
         ("ca", f"{BASE}/legal/ca", "legal/ca/index.html")],
        [("en", f"{BASE}/privacy", "privacy/index.html"),
         ("es", f"{BASE}/privacy/es", "privacy/es/index.html"),
         ("ca", f"{BASE}/privacy/ca", "privacy/ca/index.html")],
    ])

    for home, href in CSIRT_NAV.items():
        body = (ROOT / home).read_text(encoding="utf-8")
        if f'href="{href}"' not in body:
            fail(home, "no CSIRT link in navigation")

    check_security_txt()
    check_pgp_key()
    check_sitemap()
    check_no_inline_handlers()
    check_rfc_structure()
    check_retired_wording()
    check_page_rfc_consistency()
    check_csp()
    check_service_commitments()

    if errors:
        print(f"FAIL — {len(errors)} problem(s):", file=sys.stderr)
        for e in errors:
            print(f"  • {e}", file=sys.stderr)
        return 1
    print(f"OK — {len(PAGES)} pages, links, metadata, hreflang, security.txt, "
          "PGP key, sitemap, RFC 2350 structure, retired wording, page/RFC consistency,\n"
          "     CSP, service commitments")
    return 0


if __name__ == "__main__":
    sys.exit(main())
