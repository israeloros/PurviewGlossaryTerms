# Purview Glossary Bulk Loader

A menu-driven Python utility that bulk-loads glossary terms into the **Microsoft
Purview Unified Catalog (Data Governance)** using the `datagovernance` REST API.
Before loading, it detects duplicate terms — both **within the input CSV** and
**against terms already in Purview** — then logs and **skips** those duplicates.

## Features

- Menu-driven, interactive CLI — no arguments to memorize.
- Flexible CSV input with sensible defaults pulled from config.
- Duplicate detection:
  - within the input file (same name + domain), and
  - against existing Purview terms (optional, via the Terms *Query* API).
- Duplicates are logged and skipped; the load only submits unique terms.
- Resolves governance domain names using config mapping, the Domains API, and — as a fallback on some Purview instances — the `/datagovernance/catalog/businessdomains` listing (paginated).
- In-memory caching of discovered governance domains to avoid repeated lookups during a run.
- Two authentication modes, selectable in config: **Azure CLI** or **service principal**.
- Dry-run mode to preview the exact API payloads before sending.
- Timestamped log file for every session under `logs/`.

## Setup

```powershell
# 1. Create and activate a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Install dependencies
pip install -r requirements.txt

# 3. Create your config from the template and edit it
Copy-Item config.example.json config.json
notepad config.json
```

## Configuration (`config.json`)

| Key | Description |
| --- | --- |
| `endpoint` | Your Purview account endpoint, e.g. `https://<account>.purview.azure.com` |
| `api_version` | Data Governance API version (default `2026-03-20-preview`) |
| `scope` | OAuth scope (default `https://purview.azure.net/.default`) |
| `auth.method` | `azure_cli` or `service_principal` |
| `auth.tenant_id` / `client_id` / `client_secret` | Required only for `service_principal` |
| `default_domain_id` | Governance domain name or GUID used when a CSV row has no `domain`. Names will be resolved to GUIDs at runtime. |
| `default_status` | `Draft`, `Published`, or `Expired` (default `Draft`) |
| `csv_path` | Default CSV file to load |
| `duplicate_match.case_insensitive` | Match names ignoring case |
| `duplicate_match.check_against_purview` | Query Purview for existing terms during dedupe |

> **Note:** `client_secret` is a credential. Prefer `azure_cli` locally and keep
> `config.json` out of source control (it is already in `.gitignore`).

## CSV format

Only `name` is required. Column order does not matter; unknown columns are ignored.

| Column | Notes |
| --- | --- |
| `name` | **Required.** Term name. |
| `description` | Free text. |
| `domain` | Governance domain name or GUID. If a name is provided it will be translated to the corresponding GUID (via config mapping, the Purview Domains API, or a fallback to the businessdomains listing when available). Resolved names are cached in-memory during the program run. Falls back to `default_domain_id`. |
| `status` | `Draft` / `Published` / `Expired`. Falls back to `default_status`. |
| `acronyms` | Semicolon-separated, e.g. `NPS;CSAT`. |
| `parentId` | Parent term GUID (optional). |
| `owners` | Semicolon-separated owner object IDs (Entra oid). |
| `experts` | Semicolon-separated expert object IDs (Entra oid). |
| `isLeaf` | Boolean: `true`/`false` (also accepts `yes`/`no`/`1`/`0`). Blank leaves it unset. |
| `resources` | Semicolon-separated `Name|https://url` pairs. |

See `sample_terms.csv` for an example (it intentionally contains one duplicate).

## Usage

```powershell
.\.venv\Scripts\Activate.ps1
python purview_glossary_loader.py
```

Typical flow:

1. **Load / reload configuration** (auto-loaded if `config.json` exists).
2. **Load & preview CSV terms**.
3. **Detect duplicates** — review what will be skipped.
4. **Dry run** (optional) — inspect the API payloads.
5. **Bulk load terms** — confirms, then loads only the unique terms.

## Non-interactive / automation mode

Pass an action flag to run headless (ideal for CI/CD). Config values can be
overridden on the command line, and the service-principal secret can come from
the `PURVIEW_CLIENT_SECRET` environment variable.

```powershell
# Report duplicates only (offline, no Purview calls)
python purview_glossary_loader.py --csv terms.csv --no-purview-check --dedupe

# Preview the exact API payloads for the unique terms
python purview_glossary_loader.py --csv terms.csv --dry-run

# Bulk load, skipping the confirmation prompt (CI-friendly)
python purview_glossary_loader.py --config config.json --csv terms.csv --load --yes

# Service principal via environment variable, endpoint/domain overrides
$env:PURVIEW_CLIENT_SECRET = "<secret>"
python purview_glossary_loader.py `
  --auth-method service_principal --tenant-id <tid> --client-id <cid> `
  --endpoint https://<account>.purview.azure.com `
  --domain <domain-name-or-guid> --csv terms.csv --load --yes
```

Actions (mutually exclusive): `--dedupe`, `--dry-run`, `--load`. Run
`python purview_glossary_loader.py --help` for the full flag list. With no
action flag, the program starts the interactive menu.

**Exit codes:** `0` success, `1` load completed with one or more failures,
`2` configuration/parse error.


## Notes

- The Data Governance REST API is in preview; the `api_version` is configurable
  so you can move it forward as new versions ship.
- Rate limits (per Microsoft docs): create ≈200 req / 20s, query ≈800 req / 20s.
  Tune `load.delay_between_requests_seconds` if you hit throttling.
