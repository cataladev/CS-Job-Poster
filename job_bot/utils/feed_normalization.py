from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit


# This policy is independently adapted from the deterministic URL and location
# rules used by GodlyDonuts/keryx. It deliberately sanitizes links without
# fetching arbitrary application destinations.
_US_STATE_CODES = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|"
    "MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC"
)
_US_STATE_NAMES = (
    "Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|"
    "Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|"
    "Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|"
    "New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|"
    "Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|"
    "Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|District of Columbia"
)
_EXPLICIT_US = re.compile(
    rf"(?:\bUnited States\b|\bUSA\b|\bU\.S\.\b|\bUS\b|"
    rf"\bNYC\b|\bSF\b|\b(?:{_US_STATE_NAMES})\b)",
    re.IGNORECASE,
)
# Keep state abbreviations case-sensitive: case-insensitive `IN` and `OR` would turn
# ordinary prose such as "remote in Canada" into false U.S. evidence.
_US_STATE_CODE = re.compile(rf"(?:,\s*|\b)(?:{_US_STATE_CODES})\b")
_FOREIGN = re.compile(
    r"\b(?:Canada|Canadian|Toronto|Vancouver|Montreal|Calgary|Ottawa|Edmonton|Kitchener|"
    r"United Kingdom|UK|London|India|Germany|France|Ireland|Poland|Singapore|Australia|"
    r"Netherlands|Spain|Sweden|Mexico|Brazil|China|Japan|Taiwan|Israel)\b",
    re.IGNORECASE,
)
_CANADIAN_PROVINCE = re.compile(r"(?:,\s*|\b)(?:AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT)\b")
_FOREIGN_URL = re.compile(
    r"(?:[-_/](?:canada|india|united-kingdom|uk|germany|france|singapore|australia)(?:[-_/]|$)|"
    r"(?:CAD|CAN)[-_]Remote)",
    re.IGNORECASE,
)
_GENERIC_TRUSTED_LOCATION = re.compile(
    r"^(?:remote|hybrid|virtual|multiple locations?|various locations?|not specified|nationwide)$",
    re.IGNORECASE,
)

_IDENTITY_QUERY_KEYS = frozenset(
    {
        "gh_jid",
        "id",
        "jid",
        "job",
        "job_id",
        "job_posting_id",
        "jobid",
        "jobpostingid",
        "position",
        "position_id",
        "positionid",
        "posting_id",
        "req",
        "req_id",
        "reqid",
        "requisition_id",
        "requisitionid",
    }
)
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_QUERY_VALUE = re.compile(r"^[A-Za-z0-9._~-]{1,200}$")
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_DANGEROUS_ESCAPE = re.compile(r"%(?:0[0-9A-Fa-f]|1[0-9A-Fa-f]|2[35fF]|3[fF]|40|5[cC]|7[fF])")
_BLOCKED_SUFFIXES = (
    ".example",
    ".corp",
    ".home",
    ".home.arpa",
    ".internal",
    ".invalid",
    ".intranet",
    ".lan",
    ".local",
    ".localhost",
    ".onion",
    ".private",
    ".test",
)
_BLOCKED_HOSTS = frozenset(
    {
        "0.0.0.0",
        "bit.ly",
        "buff.ly",
        "cutt.ly",
        "docs.google.com",
        "drive.google.com",
        "dropbox.com",
        "forms.gle",
        "goo.gl",
        "is.gd",
        "localhost",
        "lnkd.in",
        "linktr.ee",
        "metadata.google.internal",
        "t.co",
        "ow.ly",
        "rb.gy",
        "rebrand.ly",
        "shorturl.at",
        "tinyurl.com",
        "typeform.com",
        "www.jotform.com",
    }
)
_DOWNLOAD_SUFFIXES = (
    ".app",
    ".dmg",
    ".docm",
    ".exe",
    ".iso",
    ".jar",
    ".msi",
    ".pkg",
    ".scr",
    ".xlsm",
    ".zip",
)
_AGGREGATOR_APPLICATION_HOSTS = frozenset(
    {
        "jobright.ai",
        "www.jobright.ai",
        "simplify.jobs",
        "www.simplify.jobs",
        "speedyapply.com",
        "www.speedyapply.com",
    }
)


@dataclass(frozen=True)
class SafeUrl:
    url: str
    host: str
    reason: str | None = None


def clean_feed_text(value: object) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"!\[[^]]*]\([^)]*\)", "", text)
    text = re.sub(r"\[([^]]+)]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.replace("|", "/").split()).strip(" .")


def sanitize_application_url(value: str) -> SafeUrl:
    raw = value.strip()
    if not raw or len(raw) > 4_096:
        return SafeUrl("", "", "empty-or-oversized")
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        return SafeUrl("", "", "control-character")
    if "\\" in raw:
        return SafeUrl("", "", "backslash")
    try:
        parsed = urlsplit(raw)
        host_value = parsed.hostname
        port = parsed.port
    except ValueError:
        return SafeUrl("", "", "invalid-authority")
    if parsed.scheme.casefold() not in {"http", "https"} or not host_value:
        return SafeUrl("", "", "invalid-scheme-or-host")
    if parsed.username is not None or parsed.password is not None:
        return SafeUrl("", "", "embedded-credentials")
    if port not in {None, 443}:
        return SafeUrl("", "", "nonstandard-port")
    try:
        host = host_value.encode("ascii", "strict").decode("ascii").casefold()
    except UnicodeError:
        return SafeUrl("", "", "non-ascii-host")
    if len(host) > 253 or host.endswith(".") or "." not in host:
        return SafeUrl("", host, "invalid-hostname")
    if any(not _HOST_LABEL.fullmatch(label) or label.startswith("xn--") for label in host.split(".")):
        return SafeUrl("", host, "invalid-hostname")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return SafeUrl("", host, "ip-literal")
    if (
        any(host == blocked or host.endswith(f".{blocked}") for blocked in _BLOCKED_HOSTS)
        or any(host.endswith(suffix) for suffix in _BLOCKED_SUFFIXES)
    ):
        return SafeUrl("", host, "blocked-host")

    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    escapes = "".join(_PERCENT_ESCAPE.findall(path))
    if path.count("%") != len(escapes) // 3:
        return SafeUrl("", host, "invalid-percent-escape")
    if _DANGEROUS_ESCAPE.search(path):
        return SafeUrl("", host, "encoded-delimiter-or-control")
    segments = [segment.casefold() for segment in path.split("/")]
    if any(segment in {".", "..", "%2e", "%2e%2e", ".%2e", "%2e."} for segment in segments):
        return SafeUrl("", host, "dot-segment")
    path = quote(path, safe="/%!$&*+,-._~;=:@").rstrip("/")
    if path.casefold().endswith("/apply"):
        path = path[:-6]
    if path.casefold().endswith(_DOWNLOAD_SUFFIXES):
        return SafeUrl("", host, "download-link")

    try:
        parsed_query = parse_qsl(parsed.query, max_num_fields=64)
    except ValueError:
        return SafeUrl("", host, "oversized-query")
    identity: dict[str, str] = {}
    for key, item in parsed_query:
        normalized_key = key.casefold()
        if normalized_key in _IDENTITY_QUERY_KEYS and _QUERY_VALUE.fullmatch(item):
            identity.setdefault(normalized_key, item)
    query = urlencode(sorted(identity.items()))
    sanitized = urlunsplit(("https", host, path or "/", query, ""))
    if len(sanitized) > 2_048:
        return SafeUrl("", host, "oversized-sanitized-url")
    return SafeUrl(sanitized, host)


def is_us_location(location: str, *, trusted_us: bool, context: str = "") -> bool:
    explicit_us = bool(_EXPLICIT_US.search(location))
    state_code = bool(_US_STATE_CODE.search(location))
    foreign = bool(_FOREIGN.search(location) or _CANADIAN_PROVINCE.search(location))
    if foreign:
        return False
    if _FOREIGN_URL.search(context) and not (explicit_us or state_code):
        return False
    if explicit_us or state_code:
        return True
    if _FOREIGN.search(context):
        return False
    # A feed's U.S. scope may disambiguate only genuinely generic labels. It must
    # never turn an unknown city/country such as "Asti, Italy" into a U.S. role.
    return trusted_us and bool(_GENERIC_TRUSTED_LOCATION.fullmatch(location.strip()))


def external_identity_keys(url: str, *, company: str = "") -> set[str]:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold()
    parts = [part for part in parsed.path.split("/") if part and part.casefold() != "apply"]
    query = dict(parse_qsl(parsed.query))
    keys: set[str] = set()

    if query.get("gh_jid"):
        keys.add(f"ats:greenhouse:{query['gh_jid']}")
    if "greenhouse.io" in host and "jobs" in parts:
        index = parts.index("jobs")
        if index + 1 < len(parts):
            keys.add(f"ats:greenhouse:{parts[index + 1]}")
    if host in {"jobs.lever.co", "jobs.eu.lever.co"} and len(parts) >= 2:
        keys.add(f"ats:lever:{parts[1]}")
    if host == "jobs.ashbyhq.com" and len(parts) >= 2:
        keys.add(f"ats:ashby:{parts[1]}")
    if "myworkdayjobs.com" in host or "myworkdaysite.com" in host:
        tenant = host.split(".")[0]
        requisition = parts[-1].rsplit("_", 1)[-1] if parts else ""
        if requisition:
            keys.add(f"ats:workday:{tenant}:{requisition.casefold()}")
    if company:
        stable_ids = {
            key.rsplit(":", 1)[-1].casefold()
            for key in keys
            if key.startswith("ats:")
        }
        stable_ids.update(
            {
                value.casefold()
                for name, value in query.items()
                if name != "gh_jid" and name in _IDENTITY_QUERY_KEYS and value
            }
        )
        keys.update(f"req:{company}:{value}" for value in stable_ids)
    return keys


def preferred_application_url(existing_url: str, candidate_url: str) -> str:
    """Keep a direct application destination when an aggregator copy arrives."""
    if not existing_url:
        return candidate_url
    if not candidate_url:
        return existing_url
    existing_host = (urlsplit(existing_url).hostname or "").casefold()
    candidate_host = (urlsplit(candidate_url).hostname or "").casefold()
    existing_is_aggregator = existing_host in _AGGREGATOR_APPLICATION_HOSTS
    candidate_is_aggregator = candidate_host in _AGGREGATOR_APPLICATION_HOSTS
    if existing_is_aggregator and not candidate_is_aggregator:
        return candidate_url
    return existing_url


def location_scoped_identity_keys(
    identity_keys: set[str],
    *,
    normalized_locations: list[str],
) -> set[str]:
    """Bind URL and requisition identities to the locations they describe.

    The same upstream requisition can legitimately be published once per disjoint
    location. Overlapping location sets are still merged by the role matcher.
    """
    location_payload = json.dumps(
        sorted(set(normalized_locations)) or ["unknown"],
        separators=(",", ":"),
    )
    location_digest = hashlib.sha256(location_payload.encode("utf-8")).hexdigest()[:20]
    return {
        f"{identity_key}:location:{location_digest}"
        for identity_key in identity_keys
    }


def role_signature(
    *,
    track: str,
    company: str,
    title: str,
    cycle: str | None,
    normalized_locations: list[str],
) -> str:
    payload = {
        "track": track,
        "company": company,
        "title": role_title_key(title, cycle),
        "cycle": (cycle or "").casefold(),
        "locations": sorted(set(normalized_locations)),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return f"role:{digest}"


def role_title_key(normalized_title: str, cycle: str | None) -> str:
    """Remove title tokens already represented by the separate cycle identity."""
    cycle_tokens = set(re.findall(r"[a-z0-9]+", (cycle or "").casefold()))
    removable = cycle_tokens.intersection(
        {"summer", "fall", "winter", "spring"}
        | {str(year) for year in range(2020, 2041)}
    )
    return " ".join(
        token for token in normalized_title.casefold().split() if token not in removable
    )
