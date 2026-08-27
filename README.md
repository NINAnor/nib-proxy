# NiB Proxy

A FastAPI reverse proxy for the [Norge i Bilder (NiB)](https://www.geonorge.no/nib)
WMS/WMTS services. It transparently authenticates requests against the NiB
Eksport API's token endpoint
(https://backend-api.klienter-prod-k8s2.norgeibilder.no/swagger/v1/swagger.json,
`POST /token/tilecache`) using a shared GeoID credential, reuses the
resulting token per client (HTTP `Referer`/`Origin`, falling back to the
requester's IP), and automatically refreshes the token on expiry (default
validity: 1 hour). It can also cache proxied responses in-memory (useful for
WMTS tiles) to reduce load on the upstream services.

## How it works

- Set `NIB_USERNAME` / `NIB_PASSWORD` (a GeoID user) in `.env`.
- Configure the proxied services in `services.yaml` — no code changes needed
  to add/remove upstream services. Each entry maps a `path_prefix` on the
  proxy to an `upstream` base URL, and can optionally enable response caching
  with a TTL.
- Incoming requests are matched against `services.yaml`, a NiB token is
  fetched/cached per client key (`Referer`/`Origin` host, else IP), and the
  request is forwarded upstream with `?token=<token>` appended to the query
  string. On a `401`/`403` from upstream, the token is refreshed once and
  the request is retried.
- CORS is enabled and configurable via `CORS_ALLOW_ORIGINS`/`CORS_ALLOW_METHODS`/
  `CORS_ALLOW_HEADERS`/`CORS_ALLOW_CREDENTIALS` (defaults allow any origin).
- Set `BASE_PATH` (e.g. `/nib`) to mount the proxy under a path prefix, so it
  can be exposed as `https://host/nib/...` without needing a reverse proxy
  that strips the prefix. `/healthz` remains available both prefixed and
  unprefixed for infra liveness/readiness probes.
- `GET /services` (or `GET <BASE_PATH>/services`) lists the currently
  configured upstream services (name, effective path prefix, upstream URL,
  passthrough prefix, and cache settings) for introspection/debugging.
- Set `PUBLIC_BASE_URL` (e.g. `https://example.org`) to rewrite upstream
  base URLs found in textual response bodies (WMS/WMTS Capabilities
  documents) to point back at this proxy instead, so clients keep going
  through it (and its authentication) for subsequent requests instead of
  bypassing it. Two kinds of URLs are rewritten:
  - Exact matches of a service's configured alias (any scheme/port variant)
    are rewritten to that service's friendly external URL.
  - Any other same-origin URL (e.g. ArcGIS's own canonical REST paths,
    which some upstreams embed regardless of which alias was used to fetch
    the document) is rewritten to a passthrough URL
    (`<PUBLIC_BASE_URL><BASE_PATH>/_upstream/<service-name>/<original-path>`)
    that, when followed, forwards to the exact same upstream path/host.

## Setup
Install `uv`: https://docs.astral.sh/uv/getting-started/installation/

```bash
git init
uv sync --dev
git add .
git commit -m "Initial commit"
uv run prek install # optional
cp .env.example .env  # then fill in NIB_USERNAME / NIB_PASSWORD
```


### Run
To execute your software you have two options:

**Option 1: Direct execution**
```bash
uv run main.py
```

**Option 2: Run as installed package**
```bash
uvx --from . nib_proxy
```

### Development
Just run `uv run main.py` and you are good to go!

### Update from template
To update your project with the latest changes from the template, run:
```bash
uvx --with copier-template-extensions copier update --trust
```

You can keep your previous answers by using:
```bash
uvx --with copier-template-extensions copier update --trust --defaults
```

### (Optional) prek
prek is a fast, Rust-based tool for managing git hooks (100% compatible with pre-commit). It helps ensure code quality by running checks every time you make a commit.

First, install prek:
```bash
uv tool install prek
```

If you have installed the git hooks with `pre-commit` (template version 0.2.6 and older), remove them before installing the ones provided by prek:

```
pre-commit uninstall
```

Then install git hooks:
```bash
prek install
```

To run prek on all files:
```bash
prek run --all-files
```

### How to install a package
Run `uv add <package-name>` to install a package. For example:
```bash
uv add requests
```

#### Visual studio code
If you are using visual studio code install the recommended extensions

### Development with docker
A basic docker image is already provided, run:
```bash
docker compose up --build watch
```

### Tools installed
- uv
- prek (optional)

#### What is an environment variable? and why should I use them?
Environment variables are variables that are not populated in your code but rather in the environment
that you are running your code. This is extremely useful mainly for two reasons:
- security, you can share your code without sharing your passwords/credentials
- portability, you can avoid using hard-coded values like file-system paths or folder names

you can place your environment variables in a file called `.env`, the `main.py` will read from it. Remember to:
- NEVER commit your `.env`
- Keep a `.env.example` file updated with the variables that the software expects
