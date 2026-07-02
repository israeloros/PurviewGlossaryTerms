"""
Purview Glossary Bulk Loader
============================

A menu-driven utility that bulk-loads glossary terms into the Microsoft Purview
Unified Catalog (Data Governance) using the datagovernance REST API.

Before loading, the program detects duplicate terms both within the input file
and (optionally) against terms that already exist in Purview. Duplicates are
logged and skipped from the load.

API reference:
  POST {endpoint}/datagovernance/catalog/terms        (create)
  POST {endpoint}/datagovernance/catalog/terms/query   (query/list)
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

try:
    from azure.identity import (
        AzureCliCredential,
        ClientSecretCredential,
        DefaultAzureCredential,
    )
except ImportError:  # pragma: no cover
    AzureCliCredential = ClientSecretCredential = DefaultAzureCredential = None


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG_PATH = "config.json"
LOG_DIR = "logs"
VALID_STATUSES = {"Draft", "Published", "Expired"}
# CSV columns understood by the loader. Only "name" is strictly required.
KNOWN_COLUMNS = {
    "name",
    "description",
    "domain",
    "status",
    "acronyms",
    "parentid",
    "owners",
    "experts",
    "isleaf",
    "resources",
}
MULTI_VALUE_SEP = ";"       # separates multiple acronyms / owners / resources
RESOURCE_FIELD_SEP = "|"    # separates a resource "name|url"


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def setup_logging() -> str:
    """Configure logging to both console and a timestamped file. Returns log path."""
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(
        LOG_DIR, f"glossary_load_{datetime.now():%Y%m%d_%H%M%S}.log"
    )

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(message)s"))
    console.setLevel(logging.INFO)
    logger.addHandler(console)

    return log_path


log = logging.getLogger("purview_glossary")

# Simple in-memory cache for domains discovered via the businessdomains API
_domain_cache: Dict[str, Dict[str, Any]] = {}


def getGovernanceDomainByName(endpoint, headers, DomainName=None):
    if DomainName:
        domainName = DomainName
    else:
        domainName = input("Enter the Governance Domain Name: ")

    if domainName in _domain_cache:
        cached = _domain_cache[domainName]
        print(f"Using cached Governance Domain: Name: {cached['name']} - GUID: {cached['id']} - Status: {cached['status']}")
        return cached

    search_url = f"{endpoint}/datagovernance/catalog/businessdomains?api-version=2025-09-15-preview"

    continuation_token = None
    while True:
        if continuation_token:
            search_url += f"&continuationToken={continuation_token}"

        response = requests.get(search_url, headers=headers)
        if response.status_code != 200:
            break

        data = response.json()
        
        for domain in data.get('value', []):
            # Cache every domain we see for future lookups
            _domain_cache[domain['name']] = domain
            if domainName == domain['name']:
                print(f"Found Governance Domain: Name: {domain['name']} - GUID: {domain['id']} - Status: {domain['status']}")
                return domain
            
        continuation_token = data.get("continuationToken")

        if not continuation_token:
            break
            
    return None


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class GlossaryTerm:
    """A glossary term parsed from a CSV row."""

    name: str
    description: str = ""
    domain: str = ""
    status: str = "Draft"
    acronyms: List[str] = field(default_factory=list)
    parent_id: str = ""
    owners: List[str] = field(default_factory=list)
    experts: List[str] = field(default_factory=list)
    is_leaf: Optional[bool] = None
    resources: List[Dict[str, str]] = field(default_factory=list)
    source_row: int = 0

    def dedupe_key(self, case_insensitive: bool) -> Tuple[str, str]:
        """Key used to identify duplicates: (name, domain)."""
        name = self.name.lower() if case_insensitive else self.name
        return (name.strip(), self.domain.strip())

    def to_api_payload(self) -> Dict[str, Any]:
        """Build the request body for the Terms - Create API."""
        payload: Dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "name": self.name,
            "status": self.status,
            "domain": self.domain,
        }
        if self.description:
            payload["description"] = self.description
        if self.acronyms:
            payload["acronyms"] = self.acronyms
        if self.parent_id:
            payload["parentId"] = self.parent_id
        contacts: Dict[str, Any] = {}
        if self.owners:
            contacts["owner"] = [{"id": o} for o in self.owners]
        if self.experts:
            contacts["expert"] = [{"id": e} for e in self.experts]
        if contacts:
            payload["contacts"] = contacts
        if self.is_leaf is not None:
            payload["isLeaf"] = self.is_leaf
        if self.resources:
            payload["resources"] = self.resources
        return payload


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
class Config:
    def __init__(self, data: Dict[str, Any], path: str):
        self.path = path
        self._data = data

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path, "r", encoding="utf-8") as fh:
            return cls(json.load(fh), path)

    def get(self, *keys, default=None):
        node: Any = self._data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def set(self, value, *keys) -> None:
        node = self._data
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        node[keys[-1]] = value

    def save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2)

    @property
    def raw(self) -> Dict[str, Any]:
        return self._data


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
class PurviewAuth:
    """Wraps an azure-identity credential and caches bearer tokens."""

    def __init__(self, config: Config):
        self.config = config
        self.scope = config.get("scope", default="https://purview.azure.net/.default")
        self._credential = self._build_credential()
        self._token: Optional[str] = None
        self._expires_on: float = 0.0

    def _build_credential(self):
        method = (self.config.get("auth", "method", default="azure_cli") or "").lower()
        if method == "service_principal":
            tenant = self.config.get("auth", "tenant_id")
            client = self.config.get("auth", "client_id")
            secret = self.config.get("auth", "client_secret")
            if not all([tenant, client, secret]):
                raise ValueError(
                    "Service principal auth requires tenant_id, client_id and "
                    "client_secret in config."
                )
            log.info("Using service principal authentication.")
            return ClientSecretCredential(tenant, client, secret)
        if method == "azure_cli":
            log.info("Using Azure CLI authentication.")
            return AzureCliCredential()
        log.info("Using DefaultAzureCredential.")
        return DefaultAzureCredential()

    def get_token(self) -> str:
        now = time.time()
        if self._token and now < self._expires_on - 60:
            return self._token
        token = self._credential.get_token(self.scope)
        self._token = token.token
        self._expires_on = token.expires_on
        return self._token


# --------------------------------------------------------------------------- #
# Purview API client
# --------------------------------------------------------------------------- #
class PurviewClient:
    def __init__(self, config: Config, auth: PurviewAuth):
        self.config = config
        self.auth = auth
        self.endpoint = (config.get("endpoint") or "").rstrip("/")
        self.api_version = config.get("api_version", default="2026-03-20-preview")
        self.timeout = config.get("load", "request_timeout_seconds", default=60)
        if not self.endpoint or "<your-account>" in self.endpoint:
            raise ValueError("Config 'endpoint' is not set. Update config.json.")

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.auth.get_token()}",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        sep = "&" if "?" in path else "?"
        return f"{self.endpoint}{path}{sep}api-version={self.api_version}"

    def query_terms(
        self, domain_id: str = "", name_keyword: str = "", page_size: int = 100
    ) -> List[Dict[str, Any]]:
        """Return existing terms, paging through all results."""
        url = self._url("/datagovernance/catalog/terms/query")
        results: List[Dict[str, Any]] = []
        skip = 0
        while True:
            body: Dict[str, Any] = {"top": page_size, "skip": skip}
            if domain_id:
                body["domainIds"] = [domain_id]
            if name_keyword:
                body["nameKeyword"] = name_keyword
            resp = requests.post(
                url, headers=self._headers(), json=body, timeout=self.timeout
            )
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("value", data.get("terms", []))
            if not batch:
                break
            results.extend(batch)
            if len(batch) < page_size:
                break
            skip += page_size
        return results

    def create_term(self, term: GlossaryTerm) -> Dict[str, Any]:
        url = self._url("/datagovernance/catalog/terms")
        resp = requests.post(
            url,
            headers=self._headers(),
            json=term.to_api_payload(),
            timeout=self.timeout,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"HTTP {resp.status_code}: {resp.text[:500]}"
            )
        return resp.json() if resp.text else {}

    # ---------------- Domain name / id resolution -----------------------
    def _load_domains_from_config(self) -> Dict[str, str]:
        cfg = self.config.get("domains") or {}
        if not isinstance(cfg, dict):
            return {}
        mapping: Dict[str, str] = {}
        for k, v in cfg.items():
            if isinstance(k, str) and isinstance(v, str) and v:
                mapping[k.lower()] = v
        return mapping

    def _fetch_domains_from_api(self) -> List[Dict[str, Any]]:
        """Attempt to fetch available governance domains from the Purview API.
        Returns a list of domain objects (best-effort)."""
        # Try GET /datagovernance/catalog/domains
        try:
            url = self._url("/datagovernance/catalog/domains")
            resp = requests.get(url, headers=self._headers(), timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("value", data.get("domains", []))
        except Exception:
            pass
        # Fallback: try POST query endpoint if available
        try:
            url = self._url("/datagovernance/catalog/domains/query")
            resp = requests.post(url, headers=self._headers(), json={"top": 500, "skip": 0}, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            return data.get("value", data.get("domains", []))
        except Exception:
            return []

    def _ensure_domain_map(self) -> None:
        """Populate self._domain_map from config and/or Purview API (cached)."""
        if getattr(self, "_domain_map", None):
            return
        self._domain_map = {}
        # 1) config mapping
        self._domain_map.update(self._load_domains_from_config())
        # 2) build from API if still empty or to augment
        try:
            items = self._fetch_domains_from_api()
            for item in items:
                name = (item.get("name") or item.get("displayName") or "").strip()
                did = item.get("id") or item.get("domainId") or item.get("guid") or ""
                if name and did:
                    self._domain_map.setdefault(name.lower(), did)
        except Exception:
            # keep best-effort map
            pass

    def resolve_domain_id(self, name_or_id: str) -> str:
        """Resolve a domain name or GUID to a GUID. If already a GUID, returns it.
        Raises ValueError if it cannot be resolved."""
        if not name_or_id:
            return ""
        token = name_or_id.strip()
        # quick check: valid UUID
        try:
            uuid.UUID(token)
            return token
        except Exception:
            pass
        # load mapping and lookup case-insensitively
        self._ensure_domain_map()
        did = self._domain_map.get(token.lower())
        if did:
            return did
        # Fallback: attempt to query the businessdomains listing (may be available on some Purview instances)
        try:
            domain = getGovernanceDomainByName(self.endpoint, self._headers(), DomainName=token)
            if domain and isinstance(domain, dict):
                # Normalize id fields used elsewhere
                did = domain.get('id') or domain.get('domainId') or domain.get('guid')
                if did:
                    # cache for future lookups
                    self._domain_map[token.lower()] = did
                    return did
        except Exception:
            pass
        raise ValueError(f"Unknown governance domain: {name_or_id}")

    def resolve_domains_for_terms(self, terms: List[GlossaryTerm]) -> None:
        """Translate term.domain values in-place from names to GUIDs where needed.
        Mutates the provided terms list."""
        self._ensure_domain_map()
        for term in terms:
            if not term.domain:
                continue
            try:
                term.domain = self.resolve_domain_id(term.domain)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Could not resolve domain for term '%s' (row %d): %s",
                    term.name, term.source_row, exc,
                )
                # clear the domain so it fails loudly later
                term.domain = ""


# Override PurviewClient._headers to use auth token (ensures valid Authorization)
def _purview_client_headers(self) -> Dict[str, str]:
    token = ""
    try:
        token = self.auth.get_token()
    except Exception:
        token = ""
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

PurviewClient._headers = _purview_client_headers

# --------------------------------------------------------------------------- #
# CSV parsing
# --------------------------------------------------------------------------- #
def parse_csv(path: str, config: Config) -> List[GlossaryTerm]:

    """Parse a CSV file into GlossaryTerm objects, applying config defaults."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"CSV file not found: {path}")

    default_domain = config.get("default_domain_id", default="") or ""
    default_status = config.get("default_status", default="Draft") or "Draft"

    terms: List[GlossaryTerm] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError("CSV file is empty or has no header row.")

        normalized = {c.strip().lower() for c in reader.fieldnames}
        if "name" not in normalized:
            raise ValueError("CSV must contain a 'name' column.")
        unknown = normalized - KNOWN_COLUMNS
        if unknown:
            log.warning("Ignoring unrecognized CSV columns: %s", ", ".join(sorted(unknown)))

        for i, row in enumerate(reader, start=2):  # row 1 is the header
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            name = row.get("name", "")
            if not name:
                log.warning("Row %d skipped: empty 'name'.", i)
                continue

            status = row.get("status") or default_status
            if status not in VALID_STATUSES:
                log.warning(
                    "Row %d: status '%s' is not one of %s; using '%s'.",
                    i, status, sorted(VALID_STATUSES), default_status,
                )
                status = default_status

            is_leaf = _parse_bool(row.get("isleaf", ""))
            if is_leaf is None and row.get("isleaf", ""):
                log.warning(
                    "Row %d: isLeaf value '%s' is not a recognized boolean; "
                    "leaving it unset.", i, row.get("isleaf"),
                )

            terms.append(
                GlossaryTerm(
                    name=name,
                    description=row.get("description", ""),
                    domain=row.get("domain") or default_domain,
                    status=status,
                    acronyms=_split_multi(row.get("acronyms", "")),
                    parent_id=row.get("parentid", ""),
                    owners=_split_multi(row.get("owners", "")),
                    experts=_split_multi(row.get("experts", "")),
                    is_leaf=is_leaf,
                    resources=_parse_resources(row.get("resources", "")),
                    source_row=i,
                )
            )
    return terms


def _split_multi(value: str) -> List[str]:
    return [v.strip() for v in value.split(MULTI_VALUE_SEP) if v.strip()]


def _parse_bool(value: str) -> Optional[bool]:
    """Parse a CSV boolean. Returns None when blank or unrecognized."""
    token = value.strip().lower()
    if token in ("true", "yes", "y", "1"):
        return True
    if token in ("false", "no", "n", "0"):
        return False
    return None


def _parse_resources(value: str) -> List[Dict[str, str]]:
    resources: List[Dict[str, str]] = []
    for chunk in _split_multi(value):
        if RESOURCE_FIELD_SEP in chunk:
            rname, rurl = chunk.split(RESOURCE_FIELD_SEP, 1)
        else:
            rname, rurl = chunk, chunk
        resources.append({"name": rname.strip(), "url": rurl.strip()})
    return resources


# --------------------------------------------------------------------------- #
# Duplicate detection
# --------------------------------------------------------------------------- #
@dataclass
class DedupeResult:
    unique_terms: List[GlossaryTerm]
    file_duplicates: List[Tuple[GlossaryTerm, str]]      # (term, reason)
    purview_duplicates: List[Tuple[GlossaryTerm, str]]   # (term, reason)


def detect_duplicates(
    terms: List[GlossaryTerm],
    config: Config,
    client: Optional[PurviewClient],
) -> DedupeResult:
    case_insensitive = config.get(
        "duplicate_match", "case_insensitive", default=True
    )
    check_purview = config.get(
        "duplicate_match", "check_against_purview", default=True
    )

    unique: List[GlossaryTerm] = []
    file_dupes: List[Tuple[GlossaryTerm, str]] = []
    purview_dupes: List[Tuple[GlossaryTerm, str]] = []

    # 1) Within-file duplicates
    seen: Dict[Tuple[str, str], int] = {}
    file_unique: List[GlossaryTerm] = []
    for term in terms:
        key = term.dedupe_key(case_insensitive)
        if key in seen:
            file_dupes.append(
                (term, f"duplicate of row {seen[key]} within the input file")
            )
        else:
            seen[key] = term.source_row
            file_unique.append(term)

    # 2) Duplicates against existing Purview terms
    existing_keys: set = set()
    if check_purview and client is not None:
        domains = {t.domain for t in file_unique if t.domain}
        log.info("Fetching existing terms from Purview for %d domain(s)...", len(domains))
        try:
            existing = _fetch_existing(client, domains)
            for ex in existing:
                ex_name = ex.get("name", "")
                ex_domain = ex.get("domain", "")
                key_name = ex_name.lower() if case_insensitive else ex_name
                existing_keys.add((key_name.strip(), (ex_domain or "").strip()))
            log.info("Retrieved %d existing term(s) from Purview.", len(existing))
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not query Purview for existing terms: %s", exc)
            log.warning("Proceeding with in-file duplicate detection only.")

    for term in file_unique:
        key = term.dedupe_key(case_insensitive)
        if key in existing_keys:
            purview_dupes.append(
                (term, "a term with this name already exists in Purview")
            )
        else:
            unique.append(term)

    return DedupeResult(unique, file_dupes, purview_dupes)


def _fetch_existing(client: PurviewClient, domains: set) -> List[Dict[str, Any]]:
    if domains:
        results: List[Dict[str, Any]] = []
        for domain_id in domains:
            results.extend(client.query_terms(domain_id=domain_id))
        return results
    return client.query_terms()


def log_duplicates(result: DedupeResult) -> None:
    for term, reason in result.file_duplicates:
        log.info("SKIPPED (file dup)    | row %-4d | %-40s | %s",
                 term.source_row, term.name, reason)
    for term, reason in result.purview_duplicates:
        log.info("SKIPPED (purview dup) | row %-4d | %-40s | %s",
                 term.source_row, term.name, reason)


# --------------------------------------------------------------------------- #
# Bulk load
# --------------------------------------------------------------------------- #
def bulk_load(
    terms: List[GlossaryTerm], client: PurviewClient, config: Config
) -> Tuple[int, int]:
    delay = config.get("load", "delay_between_requests_seconds", default=0.1)
    loaded = 0
    failed = 0
    total = len(terms)
    for idx, term in enumerate(terms, start=1):
        if not term.domain:
            failed += 1
            log.error("FAILED  (%d/%d) | %-40s | no domain set (config default_domain_id "
                      "is empty and CSV 'domain' is blank)", idx, total, term.name)
            continue
        try:
            client.create_term(term)
            loaded += 1
            log.info("LOADED  (%d/%d) | %-40s | domain=%s", idx, total, term.name, term.domain)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            log.error("FAILED  (%d/%d) | %-40s | %s", idx, total, term.name, exc)
        if delay:
            time.sleep(delay)
    return loaded, failed


# --------------------------------------------------------------------------- #
# Menu / interactive application
# --------------------------------------------------------------------------- #
class App:
    def __init__(self):
        self.config: Optional[Config] = None
        self.config_path: str = DEFAULT_CONFIG_PATH
        self.terms: List[GlossaryTerm] = []
        self.dedupe: Optional[DedupeResult] = None
        self._client: Optional[PurviewClient] = None

    # ---- lazy client -----------------------------------------------------
    def client(self) -> PurviewClient:
        if self._client is None:
            if self.config is None:
                raise RuntimeError("Load configuration first (menu option 1).")
            auth = PurviewAuth(self.config)
            self._client = PurviewClient(self.config, auth)
        return self._client

    # ---- menu actions ----------------------------------------------------
    def load_config(self) -> None:
        path = input(f"Config file path [{self.config_path}]: ").strip() or self.config_path
        if not os.path.isfile(path):
            print(f"  Not found: {path}")
            if os.path.isfile("config.example.json"):
                print("  Tip: copy config.example.json to config.json and edit it.")
            return
        self.config = Config.load(path)
        self.config_path = path
        self._client = None  # force rebuild with new config
        print(f"  Loaded configuration from {path}")
        self._show_config_summary()

    def _show_config_summary(self) -> None:
        if not self.config:
            print("  No configuration loaded.")
            return
        print("  --- Configuration ---")
        print(f"  Endpoint         : {self.config.get('endpoint')}")
        print(f"  API version      : {self.config.get('api_version')}")
        print(f"  Auth method      : {self.config.get('auth', 'method')}")
        print(f"  Default domain   : {self.config.get('default_domain_id') or '(none)'}")
        print(f"  Default status   : {self.config.get('default_status')}")
        print(f"  CSV path         : {self.config.get('csv_path')}")
        print(f"  Check Purview dup: {self.config.get('duplicate_match', 'check_against_purview')}")

    def set_csv_path(self) -> None:
        self._require_config()
        current = self.config.get("csv_path", default="")
        path = input(f"CSV file path [{current}]: ").strip() or current
        self.config.set(path, "csv_path")
        print(f"  CSV path set to: {path}")
        save = input("  Persist to config file? (y/N): ").strip().lower()
        if save == "y":
            self.config.save()
            print("  Saved.")

    def load_csv(self) -> None:
        self._require_config()
        path = self.config.get("csv_path", default="sample_terms.csv")
        self.terms = parse_csv(path, self.config)
        self.dedupe = None
        print(f"  Parsed {len(self.terms)} term(s) from {path}.")
        self._preview_terms()

    def _preview_terms(self, limit: int = 10) -> None:
        if not self.terms:
            print("  No terms loaded.")
            return
        print(f"  {'Row':<5} {'Name':<35} {'Status':<10} Domain")
        print(f"  {'-'*4:<5} {'-'*34:<35} {'-'*9:<10} {'-'*20}")
        for term in self.terms[:limit]:
            print(f"  {term.source_row:<5} {term.name[:34]:<35} "
                  f"{term.status:<10} {term.domain or '(none)'}")
        if len(self.terms) > limit:
            print(f"  ... and {len(self.terms) - limit} more.")

    def run_dedupe(self) -> None:
        self._require_config()
        if not self.terms:
            print("  Load a CSV first (menu option 3).")
            return
        client = None
        if self.config.get("duplicate_match", "check_against_purview", default=True):
            try:
                client = self.client()
            except Exception as exc:  # noqa: BLE001
                print(f"  Could not initialize Purview client: {exc}")
                print("  Continuing with in-file duplicate detection only.")
        if client is not None:
            try:
                client.resolve_domains_for_terms(self.terms)
            except Exception as exc:  # noqa: BLE001
                print(f"  Could not resolve domain names: {exc}")
        self.dedupe = detect_duplicates(self.terms, self.config, client)
        log_duplicates(self.dedupe)
        print("  --- Duplicate detection summary ---")
        print(f"  Total parsed        : {len(self.terms)}")
        print(f"  In-file duplicates  : {len(self.dedupe.file_duplicates)} (skipped)")
        print(f"  Purview duplicates  : {len(self.dedupe.purview_duplicates)} (skipped)")
        print(f"  Unique to load      : {len(self.dedupe.unique_terms)}")

    def run_load(self) -> None:
        self._require_config()
        if not self.terms:
            print("  Load a CSV first (menu option 3).")
            return
        if self.dedupe is None:
            print("  Running duplicate detection before load...")
            self.run_dedupe()
        to_load = self.dedupe.unique_terms
        if not to_load:
            print("  Nothing to load after removing duplicates.")
            return
        print(f"  {len(to_load)} unique term(s) will be loaded; "
              f"{len(self.dedupe.file_duplicates) + len(self.dedupe.purview_duplicates)} "
              f"duplicate(s) skipped.")
        confirm = input("  Proceed with load? (y/N): ").strip().lower()
        if confirm != "y":
            print("  Load cancelled.")
            return
        try:
            client = self.client()
        except Exception as exc:  # noqa: BLE001
            print(f"  Cannot load: {exc}")
            return
        # Resolve domain names to GUIDs before loading
        try:
            client.resolve_domains_for_terms(to_load)
        except Exception as exc:  # noqa: BLE001
            print(f"  Could not resolve domain names before load: {exc}")
            print("  Aborting load.")
            return
        loaded, failed = bulk_load(to_load, client, self.config)
        print("  --- Load complete ---")
        print(f"  Loaded : {loaded}")
        print(f"  Failed : {failed}")
        print(f"  Skipped: {len(self.dedupe.file_duplicates) + len(self.dedupe.purview_duplicates)}")

    def dry_run(self) -> None:
        """Preview the API payloads without calling Purview."""
        self._require_config()
        if not self.terms:
            print("  Load a CSV first (menu option 3).")
            return
        terms = self.dedupe.unique_terms if self.dedupe else self.terms
        for term in terms[:5]:
            print(json.dumps(term.to_api_payload(), indent=2))
        if len(terms) > 5:
            print(f"  ... and {len(terms) - 5} more payload(s).")

    def show_config(self) -> None:
        self._show_config_summary()

    # ---- helpers ---------------------------------------------------------
    def _require_config(self) -> None:
        if self.config is None:
            raise RuntimeError("No configuration loaded. Use menu option 1 first.")


MENU = """
=========================================================
  Purview Glossary Bulk Loader
=========================================================
  1) Load / reload configuration
  2) Show configuration
  3) Set CSV file path
  4) Load & preview CSV terms
  5) Detect duplicates (in-file + Purview)
  6) Dry run (preview API payloads)
  7) Bulk load terms (skips duplicates)
  0) Exit
---------------------------------------------------------"""


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="purview_glossary_loader.py",
        description="Bulk-load glossary terms into Microsoft Purview Unified Catalog. "
                    "Run with no arguments for the interactive menu, or use the flags "
                    "below for non-interactive automation.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG_PATH,
        help="Path to the JSON configuration file.",
    )
    parser.add_argument(
        "--csv", dest="csv_path",
        help="CSV file to load (overrides csv_path from config).",
    )

    # Config overrides (handy for CI where secrets come from the environment).
    parser.add_argument("--endpoint", help="Override Purview endpoint.")
    parser.add_argument("--api-version", help="Override Data Governance API version.")
    parser.add_argument("--domain", help="Override default domain name or GUID.")
    parser.add_argument(
        "--status", choices=sorted(VALID_STATUSES),
        help="Override default term status.",
    )
    parser.add_argument(
        "--auth-method", choices=["azure_cli", "service_principal", "default"],
        help="Override authentication method.",
    )
    parser.add_argument("--tenant-id", help="Service principal tenant ID.")
    parser.add_argument("--client-id", help="Service principal client ID.")
    parser.add_argument(
        "--client-secret",
        help="Service principal client secret (prefer PURVIEW_CLIENT_SECRET env var).",
    )
    parser.add_argument(
        "--no-purview-check", action="store_true",
        help="Skip duplicate detection against existing Purview terms.",
    )

    # Non-interactive actions (mutually exclusive).
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--dedupe", action="store_true",
        help="Parse the CSV and report duplicates, then exit (no load).",
    )
    action.add_argument(
        "--dry-run", action="store_true",
        help="Show the API payloads for unique terms, then exit (no load).",
    )
    action.add_argument(
        "--load", action="store_true",
        help="Detect duplicates and bulk-load the unique terms.",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the confirmation prompt when used with --load.",
    )
    return parser


def apply_cli_overrides(config: Config, args: argparse.Namespace) -> None:
    """Apply command-line and environment overrides onto a loaded config."""
    if args.csv_path:
        config.set(args.csv_path, "csv_path")
    if args.endpoint:
        config.set(args.endpoint, "endpoint")
    if args.api_version:
        config.set(args.api_version, "api_version")
    if args.domain:
        config.set(args.domain, "default_domain_id")
    if args.status:
        config.set(args.status, "default_status")
    if args.auth_method:
        config.set(args.auth_method, "auth", "method")
    if args.tenant_id:
        config.set(args.tenant_id, "auth", "tenant_id")
    if args.client_id:
        config.set(args.client_id, "auth", "client_id")
    secret = args.client_secret or os.environ.get("PURVIEW_CLIENT_SECRET")
    if secret:
        config.set(secret, "auth", "client_secret")
    if args.no_purview_check:
        config.set(False, "duplicate_match", "check_against_purview")


def run_cli(args: argparse.Namespace) -> int:
    """Non-interactive execution path. Returns a process exit code."""
    if not os.path.isfile(args.config):
        log.error("Config file not found: %s", args.config)
        return 2
    config = Config.load(args.config)
    apply_cli_overrides(config, args)

    csv_path = config.get("csv_path", default="")
    try:
        terms = parse_csv(csv_path, config)
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to parse CSV: %s", exc)
        return 2
    log.info("Parsed %d term(s) from %s.", len(terms), csv_path)
    if not terms:
        log.warning("No terms to process.")
        return 0

    # Apply offline domain name -> GUID mapping from config (if provided).
    cfg_domains = config.get("domains") or {}
    if isinstance(cfg_domains, dict) and cfg_domains:
        lower_map = {k.lower(): v for k, v in cfg_domains.items() if isinstance(k, str) and isinstance(v, str) and v}
        if lower_map:
            for t in terms:
                if not t.domain:
                    continue
                token = t.domain.strip()
                try:
                    uuid.UUID(token)
                    # already a GUID
                    continue
                except Exception:
                    pass
                did = lower_map.get(token.lower())
                if did:
                    t.domain = did

    # Build a client unless we are purely offline (dedupe/dry-run without Purview check).
    client: Optional[PurviewClient] = None
    need_client = args.load or config.get(
        "duplicate_match", "check_against_purview", default=True
    )
    if need_client:
        try:
            client = PurviewClient(config, PurviewAuth(config))
        except Exception as exc:  # noqa: BLE001
            if args.load:
                log.error("Cannot initialize Purview client: %s", exc)
                return 2
            log.warning("Purview client unavailable (%s); in-file dedupe only.", exc)

    if client is not None:
        try:
            client.resolve_domains_for_terms(terms)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not resolve domain names: %s", exc)

    result = detect_duplicates(terms, config, client)
    log_duplicates(result)
    skipped = len(result.file_duplicates) + len(result.purview_duplicates)
    log.info("Dedupe: %d parsed, %d in-file dup, %d Purview dup, %d unique.",
             len(terms), len(result.file_duplicates),
             len(result.purview_duplicates), len(result.unique_terms))

    if args.dedupe:
        return 0

    if args.dry_run:
        for term in result.unique_terms:
            print(json.dumps(term.to_api_payload(), indent=2))
        return 0

    # --load
    to_load = result.unique_terms
    if not to_load:
        log.info("Nothing to load after removing %d duplicate(s).", skipped)
        return 0
    if not args.yes:
        confirm = input(
            f"Load {len(to_load)} unique term(s) ({skipped} skipped)? (y/N): "
        ).strip().lower()
        if confirm != "y":
            log.info("Load cancelled.")
            return 0
    if client is None:
        log.error("No Purview client available; cannot load.")
        return 2
    loaded, failed = bulk_load(to_load, client, config)
    log.info("Load complete: %d loaded, %d failed, %d skipped.", loaded, failed, skipped)
    return 1 if failed else 0


def run_interactive() -> None:
    app = App()
    # Auto-load default config if present, for convenience.
    if os.path.isfile(DEFAULT_CONFIG_PATH):
        try:
            app.config = Config.load(DEFAULT_CONFIG_PATH)
            print(f"Auto-loaded {DEFAULT_CONFIG_PATH}.")
        except Exception as exc:  # noqa: BLE001
            print(f"Could not auto-load {DEFAULT_CONFIG_PATH}: {exc}")

    actions = {
        "1": app.load_config,
        "2": app.show_config,
        "3": app.set_csv_path,
        "4": app.load_csv,
        "5": app.run_dedupe,
        "6": app.dry_run,
        "7": app.run_load,
    }

    while True:
        print(MENU)
        choice = input("Select an option: ").strip()
        if choice == "0":
            print("Goodbye.")
            break
        action = actions.get(choice)
        if not action:
            print("  Invalid choice. Try again.")
            continue
        try:
            action()
        except KeyboardInterrupt:
            print("\n  Cancelled.")
        except Exception as exc:  # noqa: BLE001
            log.error("Error: %s", exc)


def main() -> int:
    args = build_arg_parser().parse_args()
    log_path = setup_logging()
    print(f"Logging to: {log_path}")

    # Any non-interactive action flag routes to the CLI path.
    if args.load or args.dry_run or args.dedupe:
        return run_cli(args)

    run_interactive()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Goodbye.")
        sys.exit(130)
