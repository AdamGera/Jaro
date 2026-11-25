"""Normalize annotation website identities and generate path-boundary rule keys."""

from dataclasses import dataclass
from ipaddress import ip_address
import re
from urllib.parse import quote, urlsplit


MAX_SITE_KEY = 2048
FORGES = frozenset(("github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "sr.ht"))
SHARED_SITES = FORGES | frozenset((
    "linear.app", "notion.so", "notion.site", "figma.com", "docs.google.com", "drive.google.com", "app.slack.com",
    "discord.com", "vercel.com", "dash.cloudflare.com", "x.com", "twitter.com", "reddit.com",
))
SHARED_CONTAINERS = frozenset(("design", "file", "board", "document", "d", "channels", "workspace", "workspaces"))
HTTP_PORT = {"http": 80, "https": 443}
UNRESERVED = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"


def annotation_site(value):
    if not isinstance(value, str) or len(value) > 255:
        return None
    try:
        host = value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    if ":" in host and "%" not in host:
        try:
            return str(ip_address(host))
        except ValueError:
            return None
    if re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*", host):
        return host
    return None


def allowed_host(host):
    if not host or host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        address = ip_address(host)
        return not (address.is_loopback or address.is_unspecified or
                    (getattr(address, "ipv4_mapped", None) and address.ipv4_mapped.is_loopback))
    except ValueError:
        return True


@dataclass(frozen=True)
class Site:
    host: str
    path: str
    scheme: str = ""
    port: int | None = None

    def key(self):
        host = f"[{self.host}]" if ":" in self.host else self.host
        port = f":{self.port}" if self.port is not None and self.port != HTTP_PORT.get(self.scheme) else ""
        scheme = f"{self.scheme}://" if self.scheme else ""
        return f"{scheme}{host}{port}{self.path}"


def site_scope(value, *, observed=False, strip_www=False):
    if not isinstance(value, str) or not value.strip() or len(value) > (8192 if observed else MAX_SITE_KEY):
        return None
    value = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 or char == "\\" for char in value):
        return None
    # The authority precedes the path. A URL in a path or query doesn't supply the scheme.
    scheme = bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value))
    if observed and not scheme:
        return None
    try:
        parsed = urlsplit(value if scheme else f"https://{value}")
        host = annotation_site(parsed.hostname)
        port = parsed.port
    except ValueError:
        return None
    if host and strip_www:
        host = host.removeprefix("www.")
    if (parsed.scheme not in HTTP_PORT or not allowed_host(host) or port == 0 or
            (not observed and (parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment))):
        return None
    path = parsed.path.rstrip("/")
    if re.search(r"%(?:2f|5c|0[0-9a-f]|1[0-9a-f]|7f)|%(?![0-9a-f]{2})", path, re.IGNORECASE):
        return None

    def escape(match):
        char = chr(int(match[0][1:], 16))
        return char if char in UNRESERVED else match[0].upper()

    path = re.sub(r"%[0-9a-fA-F]{2}", escape, path)
    if "//" in path or len(path.split("/")) > 65 or any(part in (".", "..") for part in path.split("/")):
        return None
    path = quote(path, safe="/%!$&'()*+,;=:@")
    key = Site(host, path, parsed.scheme if scheme else "", port)
    return key if len(key.key()) <= MAX_SITE_KEY else None


def project_depth(site):
    first = site.path.lstrip("/").split("/", 1)[0].casefold()
    return 2 if site.host in FORGES or (site.host in SHARED_SITES and first in SHARED_CONTAINERS) else 1


def allowed_scope(site):
    return site.host not in SHARED_SITES or site.path.count("/") >= project_depth(site)


def site_choices(site):
    parts = site.path.lstrip("/").split("/") if site.path else []
    depth = project_depth(site)
    port = site.port if site.port != HTTP_PORT[site.scheme] else None
    if len(parts) >= depth:
        yield Site(site.host, "/" + "/".join(parts[:depth]), port=port).key()
    if site.host not in SHARED_SITES:
        yield Site(site.host, "", port=port).key()


def site_candidates(location):
    site = site_scope(location, observed=True)
    if not site:
        return
    port = site.port or HTTP_PORT[site.scheme]
    path = site.path
    while allowed_scope(Site(site.host, path)):
        yield Site(site.host, path, site.scheme, port).key()
        yield Site(site.host, path, port=port).key()
        yield Site(site.host, path).key()
        if not path:
            break
        path = path.rsplit("/", 1)[0]
