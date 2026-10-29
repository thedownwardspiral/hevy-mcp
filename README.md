# hevy-mcp

A small, self-hosted, read-only [MCP](https://modelcontextprotocol.io) server for your [Hevy](https://www.hevyapp.com) workouts and body measurements. Once it's running, an assistant such as Claude can answer questions like "how has my bench press moved over the last three months?" or "what's my weight trend since spring?".

- Built with [FastMCP](https://gofastmcp.com) 4.x and served over Streamable HTTP at `/mcp`, with a health check at `/health`.
- Runs as a Docker container behind an existing Traefik instance. No host port is published.
- Reads the Hevy API key from HashiCorp Vault at startup, logging in with AppRole. The key is never baked into the image, written to an env file or logged.
- Every tool is read-only and annotated `readOnlyHint`. Nothing can change your Hevy data.
- Reachable from Claude anywhere, including the mobile app, through a Cloudflare Tunnel. Cloudflare Access signs you in with Google, and the server checks the JWT Access signs on every request. Without the Access settings the server is unauthenticated, which is only for local development.

## How requests reach it

Claude's connector calls don't come from your phone or laptop. They come from Anthropic's cloud (outbound range `160.79.104.0/21`). Your browser only gets involved once, to sign in.

```text
Claude (claude.ai, Desktop, mobile)
  └─ Anthropic's cloud, 160.79.104.0/21
       └─ Cloudflare edge, https://hevy-mcp.example.com/mcp
            ├─ WAF custom rule: block unless the source is in an allowed range
            ├─ Access (Managed OAuth): no valid token → 401 + discovery metadata
            │    sign-in: your browser → <team>.cloudflareaccess.com → Google
            └─ Tunnel route ^/mcp, Host header set to the public hostname
                 └─ Traefik → hevy-mcp container
                      ├─ Cf-Access-Jwt-Assertion checked: signature, aud, iss, exp, email
                      └─ tool runs → Hevy API (API key read from Vault at startup)
```

There are four layers, and each one fails closed:

| Layer | What it stops |
|---|---|
| **WAF IP rule** | Everyone outside Anthropic's range and your own. Any Claude user's connector traffic comes from Anthropic's range too, so this is an extra layer, not authentication |
| **Access policy** | Anyone who can't sign in to Google as the allowed email. Sign-in uses Managed OAuth: Access is the OAuth server Claude registers with, and Claude only ever holds an opaque Access token |
| **Server JWT check** | Requests that reach the server without passing through Access, for example through a misconfigured route. Every request except `/health` needs a valid Access JWT for the allowed email |
| **Read-only tools** | Changes to your Hevy data, even by an authorized caller |

[ADR 0001](docs/adr/0001-cloudflare-access-for-remote-clients.md) records why it's built this way and which alternatives were rejected.

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

The examples use placeholders:

- `vault.<home-domain>`: Vault.
- `hevy-mcp.home.example`: the server's home-network name.
- `hevy-mcp.example.com`: its public hostname on Cloudflare.
- `<team>.cloudflareaccess.com`: your Cloudflare Zero Trust team domain.
- `you@gmail.com`: the Google account you sign in with.

Keep the real values in the gitignored `.env` and `compose.override.yml` on the Docker host, never in this repository.

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
- `MCP_ALLOWED_HOSTS`: the hostnames the server answers for, comma-separated. Use the public hostname, plus the LAN hostname if Traefik routes one. Any other Host header gets `421 Misdirected Request` before anything else runs, which blocks DNS-rebinding attacks and traffic that reaches the container some other way. Loopback names are allowed only on `/health`, so the container's health check keeps working.

The tunnel sends requests with the **public** hostname in the Host header (step 6), so Traefik needs a router for that name as well. Add one in `compose.override.yml`, or set `HEVY_MCP_HOSTNAME` to the public hostname if the server is only reached through the tunnel.

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

Until step 6 is done, the logs warn that Cloudflare Access isn't configured and the server accepts requests without authentication. Don't route the tunnel to it before then.

To deploy a new version, pull and restart. To pin a version, set `HEVY_MCP_TAG` to a release (`1.2.0`) or a commit (`sha-1a2b3c4`). Rotating the key in Vault also needs a restart, because the key is read once at startup.

### 6. Put it behind Cloudflare Access

This needs a Cloudflare account with the domain on it, a Zero Trust organization (its team domain is under **Zero Trust → Settings**), and a Cloudflare Tunnel already running somewhere that can reach Traefik. Do the steps in this order, so the public hostname is protected before the tunnel sends anything to the server.

#### 6a. Google as the login method

In [Google Cloud Console](https://console.cloud.google.com), in a project of its own:

1. **Google Auth Platform → Branding / Get started.** Give an app name and your email, and choose **External** as the audience. Internal only works for Google Workspace.
2. **Audience.** Leave the publishing status as **Testing** and add `you@gmail.com` under **Test users**. Only test users can sign in, so this is a second allow-list on Google's side. Cloudflare only asks for `openid`, `email` and `profile`, which need no verification or billing account, so you can ignore the Project Checkup warnings.
3. **Clients → Create client**, type **Web application**:
   - Authorized JavaScript origin: `https://<team>.cloudflareaccess.com`
   - Authorized redirect URI: `https://<team>.cloudflareaccess.com/cdn-cgi/access/callback`

   Copy the client ID and secret straight away, because Google may not show the secret again.

Then, in Cloudflare, go to **Zero Trust → Integrations → Identity providers → Add new → Google**. Paste the client ID (labelled **App ID**) and the secret, turn on **PKCE**, then **Save** and **Test**.

#### 6b. The Access application

**Zero Trust → Access controls → Applications → Add an application → Self-hosted and private**:

- **Destination:** public hostname `hevy-mcp.example.com`, with the path left blank so the whole host is covered.
- **Session duration:** this is also how often Claude asks you to sign in again, because Managed OAuth's grant session defaults to "Same as session duration". `168h` means about once a week.
- **Policy:** **Allow**, with **Include → Emails →** `you@gmail.com` and nothing else.
- **Login methods:** Google only, with **Instant Auth** on.
- **Advanced settings → Managed OAuth:** on.
  - **Allowed redirect URIs:** `https://claude.ai/api/mcp/auth_callback`, which claude.ai on the web, Desktop and mobile all use.
  - **Allow localhost clients** and **Allow loopback clients:** on, for Claude Code, which redirects to a local port that changes every session.
  - **Grant session duration:** "Same as session duration". **Access token lifetime:** Default. Claude refreshes the token in the background.

Save, then copy the application's **AUD tag** from its overview. The API also returns it as `aud` from `GET /accounts/{account_id}/access/apps`.

Saving the application protects the hostname straight away. Requests without a token get a `401` from Cloudflare and never reach the tunnel.

#### 6c. Turn on the server's check

Fill in the three Access settings in `.env`. Set all three together: setting only some of them stops startup.

```dotenv
CF_ACCESS_TEAM_DOMAIN=<team>.cloudflareaccess.com
CF_ACCESS_AUD=<AUD tag>
CF_ACCESS_ALLOWED_EMAILS=you@gmail.com
```

Check that all three reach the container with your values:

```bash
docker compose config | grep CF_ACCESS
```

The three lines are always there. A line that shows `""` means that setting is still blank. Variables exported in your shell take precedence over `.env`, so a blank or old value can also come from there.

Then recreate the container so it picks them up. `docker compose restart` keeps the old environment:

```bash
docker compose up -d
```

The startup warning about Access goes away. From now on every request except `/health` needs a valid Access JWT, on every route, the home-network one included.

#### 6d. The tunnel route

In the tunnel's **Published application routes** (or **Public hostnames**):

- **Hostname:** `hevy-mcp.example.com`, with **Path** `^/mcp`. Only the MCP endpoint goes through the tunnel, so `/health` and everything else never leave your network.
- **Service:** `https://hevy-mcp.home.example` (Traefik), or the container directly.
- **HTTP Host Header:** `hevy-mcp.example.com`, so Traefik's router for the public name matches (step 4).
- **Origin TLS:** Traefik's certificate has to be valid for the public hostname. Otherwise you have to turn on **No TLS Verify**, which leaves that hop on your network unverified.

#### 6e. WAF and rate limiting

Under **Security → WAF** for the domain:

- **Custom rule:** block requests whose host is `hevy-mcp.example.com` unless the source IP is in `160.79.104.0/21` (Anthropic) or a range of your own.
  - This doesn't break sign-in. Claude fetches the discovery metadata from Anthropic's IPs, and your browser signs in on `<team>.cloudflareaccess.com`, a different host the rule doesn't cover.
  - Don't put the IP condition in the Access policy instead. Policies are also checked when your browser signs in, from your phone's IP.
- **Rate-limiting rule:** one rule on `http.host eq "hevy-mcp.example.com"` (the Free plan allows one). It caps how hard a misbehaving client can hit your Hevy API quota.

#### 6f. Check it

```bash
# No token: 401 from Cloudflare, pointing at the resource metadata
curl -si -X POST https://hevy-mcp.example.com/mcp | grep -iE '^HTTP|www-authenticate'

# Discovery: the authorization server is your team domain
curl -s https://hevy-mcp.example.com/.well-known/oauth-authorization-server

# Straight to the origin, bypassing Access: 403 from the server itself
curl -sk -X POST -H 'Host: hevy-mcp.example.com' https://hevy-mcp.home.example/mcp
```

The last one should print `{"error":"Access denied."}`.

### 7. Add it to Claude

**Claude on the web, Desktop and mobile** share connectors:

1. On claude.ai, go to **Settings → Connectors → Add custom connector** and enter `https://hevy-mcp.example.com/mcp`.
2. Claude opens Access's sign-in page. Sign in with Google as `you@gmail.com`.
3. The connector now works in Claude Desktop and the Claude mobile app as well. You sign in again when the Access session ends.

**Claude Code** connects to the same public URL and signs in through Access in your browser, using a local redirect. This setup hasn't been tested end to end yet.

```bash
claude mcp add --transport http hevy https://hevy-mcp.example.com/mcp
```

Once Access is configured, the home-network hostname only answers `/health`. Every MCP request has to come through Cloudflare with a valid Access JWT.

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
| `CF_ACCESS_TEAM_DOMAIN` | none | Cloudflare Access team domain, such as `<team>.cloudflareaccess.com`. With the next two settings, every request except `/health` needs a valid Access JWT. Without them the server is unauthenticated. See [ADR 0001](docs/adr/0001-cloudflare-access-for-remote-clients.md) |
| `CF_ACCESS_AUD` | none | The Access application's AUD tag |
| `CF_ACCESS_ALLOWED_EMAILS` | none | Comma-separated emails allowed to call the server (case-insensitive) |
| `TZ` | `UTC` | Timezone for date-only inputs |
| `LOG_LEVEL` | `INFO` | Log level for stdout |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `8000` | Bind address inside the container |
| `MCP_ALLOWED_HOSTS` | none (any host) | Comma-separated hostnames the server answers for. Others get `421`. Loopback is allowed only on `/health`. Exact hostnames only: no URLs, ports or wildcards |

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
