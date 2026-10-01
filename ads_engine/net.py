"""Biztonságos letöltés a projektek oldalairól (SSRF-védelem).

A projektek brief-je és képei a projekt SAJÁT oldaláról jönnek (Ads Pack), ezért:
  · csak https (a próbáknál http is engedhető), felhasználónév/jelszó az URL-ben tilos
  · csak a motor konfigjában (projects.toml) engedélyezett gazdagépek – a briefben ez nem írható felül
  · a gazdagép nem oldódhat privát, loopback, link-local vagy fenntartott címre (belső hálózat védelme)
  · minden átirányítást újra ellenőrzünk (legfeljebb 3), a méret és az idő korlátos
"""
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request

from . import version


class FetchError(Exception):
    pass


class Fetched:
    def __init__(self, status, headers, body, url):
        self.status, self.headers, self.body, self.url = status, headers, body, url

    def text(self):
        return self.body.decode("utf-8", "replace")


def is_public_ip(value):
    ip = ipaddress.ip_address(value)
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def check_url(url, allowed_hosts, *, allow_private=False, allow_http=False):
    u = urllib.parse.urlsplit(url)
    schemes = ("https", "http") if allow_http else ("https",)
    if u.scheme not in schemes:
        raise FetchError(f"csak https cím engedélyezett: {url[:80]}")
    if u.username or u.password:
        raise FetchError("felhasználónév/jelszó az URL-ben nem engedélyezett")
    host = (u.hostname or "").lower()
    allowed = {h.lower() for h in allowed_hosts}
    if not host or host not in allowed:
        raise FetchError(f"a(z) {host or '?'} gazdagép nincs az engedélyezettek között ({', '.join(sorted(allowed)) or 'üres lista'})")
    if not allow_private and u.port not in (None, 80, 443):
        raise FetchError("nem szabványos port nem engedélyezett")
    try:
        infos = socket.getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise FetchError(f"a(z) {host} nem oldható fel: {e}") from None
    if not allow_private:
        for info in infos:
            if not is_public_ip(info[4][0]):
                raise FetchError(f"a(z) {host} nem nyilvános címre mutat ({info[4][0]})")
    return u


class _Guard(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts, allow_private, allow_http, max_redirects):
        self.allowed_hosts, self.allow_private, self.allow_http, self.max_redirects = allowed_hosts, allow_private, allow_http, max_redirects
        self.hops = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.hops += 1
        if self.hops > self.max_redirects:
            raise FetchError("túl sok átirányítás")
        check_url(newurl, self.allowed_hosts, allow_private=self.allow_private, allow_http=self.allow_http)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, allowed_hosts, *, max_bytes=5_000_000, timeout=20, headers=None, allow_private=False, allow_http=False, max_redirects=3, truncate=False):
    """Letöltés a szabályok szerint. 304 (ETag egyezés) esetén üres törzzsel tér vissza. FetchError minden szabálysértésre.

    truncate=True: a max_bytes-nál nagyobb választ nem hiba, csak az elejét olvassa (az oldal-elérhetőségi őrnek elég az állapot és a
    végső cím; a nagy – pl. egyfájlos PWA – oldal ettől még elérhető)."""
    check_url(url, allowed_hosts, allow_private=allow_private, allow_http=allow_http)
    hdrs = {"User-Agent": f"ads-engine/{version.version()}", "Accept": "*/*"}
    hdrs.update(headers or {})
    opener = urllib.request.build_opener(_Guard(allowed_hosts, allow_private, allow_http, max_redirects))
    try:
        with opener.open(urllib.request.Request(url, headers=hdrs), timeout=timeout) as r:
            clen = r.headers.get("Content-Length")
            if not truncate and clen and clen.isdigit() and int(clen) > max_bytes:
                raise FetchError(f"a fájl túl nagy ({int(clen)} bájt, a korlát {max_bytes})")
            body = r.read(max_bytes + 1)
            if len(body) > max_bytes:
                if not truncate:
                    raise FetchError(f"a fájl túl nagy (több mint {max_bytes} bájt)")
                body = body[:max_bytes]
            return Fetched(r.status, {k.lower(): v for k, v in r.headers.items()}, body, r.geturl())
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return Fetched(304, {k.lower(): v for k, v in e.headers.items()}, b"", url)
        raise FetchError(f"HTTP {e.code}: {url[:100]}") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise FetchError(f"a letöltés nem sikerült: {getattr(e, 'reason', e)}") from None
