# hevy-mcp

A small, self-hosted, read-only [MCP](https://modelcontextprotocol.io) server for your [Hevy](https://www.hevyapp.com) workouts and body measurements. Once it's running, an assistant such as Claude can answer questions like "how has my bench press moved over the last three months?" or "what's my weight trend since spring?".

- Built with [FastMCP](https://gofastmcp.com) 4.x and served over Streamable HTTP at `/mcp`, with a health check at `/health`.
- Runs as a Docker container behind an existing Traefik instance. No host port is published.
- Reads the Hevy API key from HashiCorp Vault at startup, logging in with AppRole. The key is never baked into the image, written to an env file or logged.
- Every tool is read-only and annotated `readOnlyHint`. Nothing can change your Hevy data.
- There is no authentication of its own. Expose it only on your home network.

## Tools

| Tool | What it returns |
|---|---|
| `get_user_info` | Profile, Pro status, preferred `weight_unit` and `distance_unit` |
| `list_workouts(page, page_size)` | Workouts newest first, with `page` and `page_count` |
| `get_workout(workout_id)` | One workout with every exercise and set |
| `get_workout_count()` | Total number of workouts |
| `get_workouts_in_range(start_date, end_date)` | Every workout in a date range |
| `get_training_summary(start_date, end_date)` | Workout count, duration, volume, sets and reps, per-exercise and per-muscle-group totals (warm-ups excluded) |
| `list_workout_events(since, page, page_size)` | Workout updates and deletions since a date |
| `get_exercise_history(exercise_template_id, start_date?, end_date?)` | Every logged set of one exercise |
| `search_exercise_templates(query)` | Templates whose title contains the query (case-insensitive) |
| `list_exercise_templates(page, page_size)` / `get_exercise_template(id)` | Templates with muscle groups and equipment |
| `list_body_measurements(page, page_size)` / `get_body_measurement(date)` | Body measurements |
| `get_body_measurements_in_range(start_date, end_date)` | Every body measurement in a date range |
| `list_routines` / `get_routine` / `list_routine_folders` / `get_routine_folder` | Routines and their folders |

Results keep Hevy's field names, units included (`weight_kg`, `distance_meters`). Page sizes above Hevy's limits (10, or 100 for exercise templates) are capped rather than rejected. Dates are ISO 8601, and a date-only value covers that whole day in the server's `TZ`. Range tools page through Hevy lazily and stop at the start date. They walk at most `HEVY_RANGE_MAX_PAGES` pages and set `truncated: true` if they stop early. Short Hevy failures (429, 5xx, timeouts) are retried with jittered backoff.

## Setup

The examples use placeholders: `vault.<home-domain>` for Vault and `hevy-mcp.home.example` for the server. Keep the real values in the gitignored `.env` and `compose.override.yml` on the Docker host, never in this repository.

### 1. Get a Hevy API key

The API is only available with Hevy Pro. Create a key at <https://hevy.com/settings?developer>. If Pro lapses, every tool returns an auth error.

### 2. Store the key in Vault

This assumes a KV v2 engine mounted at `secret`. Passing `api_key=-` reads the value from stdin, so it stays out of your shell history:

```bash
vault kv put secret/hevy-mcp api_key=-
```

### 3. Create the policy and the AppRole

The policy allows `read` on that one secret and nothing else:

```bash
vault policy write hevy-mcp - <<'EOF'
path "secret/data/hevy-mcp" {
  capabilities = ["read"]
}
EOF
```

The server logs in once at startup and makes a single read, so its token can be short-lived. The secret ID must not expire, because a restarted container logs in again with the same one. Optionally, bind it to the Docker host's address with `secret_id_bound_cidrs` and `token_bound_cidrs`.

```bash
vault auth enable approle   # once per Vault
vault write auth/approle/role/hevy-mcp \
  token_policies=hevy-mcp token_ttl=5m token_max_ttl=10m secret_id_ttl=0
```

### 4. Prepare the Docker host

On the Docker host, make a directory holding [`compose.yml`](compose.yml), a `.env` copied from [`env.example`](env.example), and a `secrets/` directory. Write the AppRole credentials and your internal CA bundle into `secrets/`. The container runs as UID 10001, so that user must be able to read them:

```bash
mkdir -p secrets
vault read -field=role_id auth/approle/role/hevy-mcp/role-id > secrets/vault_role_id
vault write -f -field=secret_id auth/approle/role/hevy-mcp/secret-id > secrets/vault_secret_id
cp /path/to/home-ca.pem secrets/vault-ca.pem
sudo chown 10001 secrets/vault_role_id secrets/vault_secret_id
chmod 0400 secrets/vault_role_id secrets/vault_secret_id
```

Fill in `.env`:

- `VAULT_ADDR`: Vault as the Docker host reaches it, e.g. `https://vault.<home-domain>`.
- `HEVY_MCP_HOSTNAME`: the hostname Traefik routes to the server.
- `TRAEFIK_NETWORK`: the external Docker network Traefik is attached to.
- `TRAEFIK_ENTRYPOINT`: an entrypoint that is reachable only from the home network.
- `TRAEFIK_TLS`: `true` if that entrypoint serves TLS. If Traefik issues certificates through a resolver, add `traefik.http.routers.hevy-mcp.tls.certresolver` in `compose.override.yml`.

The container has to resolve and reach Vault. Names under a home domain often need a home DNS server that containers don't use by default. If startup reports that Vault can't be reached, uncomment the `dns:` or `extra_hosts:` entry in `compose.yml` (or set it in `compose.override.yml`).

### 5. Pull and start

The image is `ghcr.io/thedownwardspiral/hevy-mcp`. If the GHCR package is private, log in once with a personal access token limited to `read:packages`:

```bash
docker login ghcr.io
```

```bash
docker compose pull && docker compose up -d
```

```bash
docker compose logs -f hevy-mcp
```

If Vault is unreachable, refuses the login, or lacks the secret or its `api_key` field, the server exits at startup with a message naming the Vault address and path. The message never contains a credential. Once it's running, `docker compose ps` shows it as `healthy`.

To deploy a new version, pull and restart. To pin a version, set `HEVY_MCP_TAG` to a release (`1.2.0`) or a commit (`sha-1a2b3c4`). Rotating the key in Vault also needs a restart, because the key is read once at startup.

### 6. Add it to Claude

The server is reachable only on your home network, so connect from a machine on that network.

**Claude Code:**

```bash
claude mcp add --transport http hevy https://hevy-mcp.home.example/mcp
```

**Claude Desktop:** Desktop connects to remote connectors from Anthropic's cloud, which can't reach your home network. Bridge to the server locally with [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) in `claude_desktop_config.json` instead. Set `NODE_EXTRA_CA_CERTS` if the certificate comes from your internal CA:

```json
{
  "mcpServers": {
    "hevy": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://hevy-mcp.home.example/mcp"],
      "env": { "NODE_EXTRA_CA_CERTS": "/path/to/home-ca.pem" }
    }
  }
}
```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `VAULT_ADDR` | none (required unless `HEVY_API_KEY` is set) | Vault address |
| `VAULT_ROLE_ID` / `VAULT_ROLE_ID_FILE` | none | AppRole role ID, or a file holding it |
| `VAULT_SECRET_ID` / `VAULT_SECRET_ID_FILE` | none | AppRole secret ID, or a file holding it |
| `VAULT_CACERT` | system CAs | CA bundle used to verify Vault's certificate. Verification is never turned off |
| `VAULT_APPROLE_MOUNT` | `approle` | AppRole auth mount |
| `VAULT_KV_MOUNT` / `VAULT_KV_PATH` | `secret` / `hevy-mcp` | KV v2 secret holding the `api_key` field |
| `HEVY_API_KEY` | none | Local-development override: when set, Vault is skipped |
| `HEVY_BASE_URL` | `https://api.hevyapp.com` | Hevy API |
| `HEVY_TIMEOUT_SECONDS` | `10` | Per-request timeout |
| `HEVY_MAX_RETRIES` / `HEVY_RETRY_BACKOFF_SECONDS` | `3` / `0.5` | Retries for 429, 5xx and timeouts |
| `HEVY_RANGE_MAX_PAGES` | `50` | Most pages a range tool walks (500 workouts) |
| `HEVY_TEMPLATE_CACHE_SECONDS` | `600` | How long exercise templates are cached |
| `TZ` | `UTC` | Timezone for date-only inputs |
| `LOG_LEVEL` | `INFO` | Log level for stdout |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `8000` | Bind address inside the container |

## Development

You need [uv](https://docs.astral.sh/uv/). It installs Python 3.14 if needed.

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Run the server against the real Hevy API without Vault:

```bash
HEVY_API_KEY=<your key> uv run hevy-mcp
```

Tests drive the real server through FastMCP's in-memory client. Hevy and Vault HTTP are faked through an injectable `httpx` transport, using synthetic fixtures modelled on Hevy's OpenAPI examples. No test calls the real Hevy API or Vault. This repository is public, so never commit real workout data.

Enable the gitleaks pre-commit hook once per clone:

```bash
pre-commit install
```

## CI and releases

- **Test** runs ruff and pytest on every pull request and push to `main`.
- **Image** runs the tests, then builds the image. On pull requests it only builds the image and checks that it becomes healthy. On `main` it publishes `latest` and `sha-<short sha>` to GHCR. A `v*` tag publishes the version too, e.g. `1.2.0` and `1.2`. To release, tag a commit:

  ```bash
  git tag v1.2.0 && git push origin v1.2.0
  ```

- **Security** runs gitleaks, CodeQL and dependency review. **zizmor** lints the workflows.
- **Dependabot** keeps the GitHub Actions, the uv dependencies and the base-image digests up to date.
