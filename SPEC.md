# Spec: Hevy MCP server

This is the spec from issue #2, updated to match the deployment. The server was first meant to be unauthenticated and reachable only on the home network. It's now reachable from Claude anywhere through a Cloudflare Tunnel and Cloudflare Access, for the reasons in [ADR 0001](docs/adr/0001-cloudflare-access-for-remote-clients.md).

## Problem Statement

I log my training and body measurements in Hevy, but the only way to look at that data is the Hevy app. When I'm talking to an AI assistant, I can't ask "how has my bench press moved over the last three months?", "how many times did I train last month?" or "what's my weight trend since spring?" without exporting data by hand. I want my assistant to read my Hevy data directly, including from the Claude app on my phone. I also want it to run on my own home server alongside the rest of my self-hosted services, not on a third-party service.

## Solution

A small, self-hosted MCP server that exposes my Hevy data as read-only MCP tools. It runs as a Docker container on my home server and sits behind my existing Traefik instance, which routes a hostname on my home network to it. It talks to Hevy's public API with my personal Hevy API key, which it reads at startup from my HashiCorp Vault at `vault.<home-domain>`. Vault runs in my k3s cluster, but the MCP server does not. It is a plain Docker container on the Docker host and reaches Vault over the network like any other client outside the cluster. The image is built and published to GitHub Container Registry (GHCR), and my server pulls it from there. MCP clients (Claude on the web, Desktop and mobile, and Claude Code) connect to it over Streamable HTTP at a public hostname, through a Cloudflare Tunnel, so nothing on the home network accepts connections from the internet. Cloudflare Access is the OAuth server clients sign in through, with Google as the only login method and a policy that allows only my email. The server holds no OAuth state of its own, but it checks the JWT that Access forwards on every request except `/health`, so a misconfigured tunnel or Access application fails closed.

## User Stories

1. As the home-lab owner, I want to run the MCP server as a Docker container, so that it fits how I already run my other services.
2. As the home-lab owner, I want the container to join my existing Traefik network and be routed by Traefik labels, so that I don't have to publish a host port.
3. As the home-lab owner, I want to reach the server at a friendly public hostname, so that I can add it to MCP clients by URL wherever they run.
4. As the home-lab owner, I want the server to read my Hevy API key from my Vault at `vault.<home-domain>` at startup, so that the key lives in one managed place and is never baked into the image, written to an env file or committed to source control.
5. As the home-lab owner, I want the server to refuse to start with a clear message if it can't reach Vault, can't log in to it or can't find the Hevy API key there, so that I find the misconfiguration right away rather than on the first tool call.
6. As the home-lab owner, I want the API key never to appear in logs, errors or tool output, so that it can't leak through my assistant's transcript.
7. As the home-lab owner, I want a health endpoint and a Docker health check, so that Docker and Traefik know when the service is ready or unhealthy.
8. As the home-lab owner, I want the container to run as a non-root user from a slim base image, so that its attack surface stays small.
9. As the home-lab owner, I want the container to restart automatically after a crash or reboot, so that the tools stay available without my attention.
10. As the home-lab owner, I want the FastMCP version pinned to the current major (4.x), so that rebuilds are reproducible and a future major release doesn't break the server silently.
11. As the home-lab owner, I want logs written to stdout at a configurable level, so that I can read them with `docker logs` like my other containers.
12. As an MCP client user, I want to connect over Streamable HTTP at a single `/mcp` endpoint, so that standard MCP clients work without custom setup.
13. As an MCP client user, I want every tool to have a clear name, description and typed parameters, so that the assistant picks the right tool and calls it correctly.
14. As an MCP client user, I want all tools to be read-only and marked that way, so that the assistant can't change or delete my Hevy data by accident.
15. As a Hevy user, I want to fetch my Hevy profile (name, username, preferred weight and distance units, Pro status), so that the assistant can present numbers in my preferred units.
16. As a Hevy user, I want to list my workouts newest first with pagination, so that the assistant can browse my recent training.
17. As a Hevy user, I want to get one workout's full details (exercises, sets, weights, reps, RPE, duration, distance), so that I can ask about a specific session.
18. As a Hevy user, I want to get my total workout count, so that I can ask how many sessions I've logged.
19. As a Hevy user, I want to get all workouts between two dates, so that I can ask questions like "what did I do last week?" without the assistant paging by hand.
20. As a Hevy user, I want a training summary for a date range (number of workouts, total training time, total volume, sets per exercise, most-trained muscle groups), so that I can see the shape of a training block at a glance.
21. As a Hevy user, I want to get the history of a specific exercise over a date range (every set with weight, reps, RPE and workout date), so that I can track progress on lifts like squat, bench and deadlift.
22. As a Hevy user, I want to search exercise templates by name (for example "bench press"), so that the assistant can find the exercise template ID it needs for exercise history without me knowing IDs.
23. As a Hevy user, I want to list and get exercise templates, including primary and secondary muscle groups and equipment, so that the assistant can reason about which muscles I'm training.
24. As a Hevy user, I want to list my body measurements with pagination, so that I can see my weight, body-fat and circumference history.
25. As a Hevy user, I want to get my body measurement for a specific date, so that I can ask "what did I weigh on 1 June?".
26. As a Hevy user, I want to get body measurements between two dates, so that I can ask about trends such as weight change over a period.
27. As a Hevy user, I want to list and get my routines and routine folders, so that the assistant can compare what I planned with what I actually did.
28. As a Hevy user, I want to list workout change events (updates and deletions) since a date, so that the assistant can tell me what changed recently.
29. As an MCP client user, I want tool results to keep Hevy's explicit unit-suffixed field names (for example `weight_kg` and `distance_meters`), so that the assistant never confuses units.
30. As an MCP client user, I want paginated tools to return the current page and the total page count, so that the assistant knows whether more data exists.
31. As an MCP client user, I want page sizes above Hevy's limits to be capped at the limit rather than rejected, so that the assistant's calls don't fail over a harmless parameter.
32. As an MCP client user, I want a clear tool error when the API key is rejected (including the hint that Hevy's API needs Hevy Pro), so that I know how to fix it.
33. As an MCP client user, I want a clear "not found" tool error when a workout, routine, template or measurement ID or date doesn't exist, so that the assistant can recover instead of guessing.
34. As an MCP client user, I want short-lived Hevy failures (rate limits, 5xx, timeouts) retried a few times with backoff before an error is returned, so that occasional flakiness doesn't break a conversation.
35. As an MCP client user, I want invalid inputs (bad dates, an end date before the start date) rejected with a helpful message, so that the assistant can correct its call.
36. As the home-lab owner, I want the server to make only the Hevy calls a tool needs and to page lazily for range queries, so that it respects Hevy's fair-use request.
37. As a developer, I want a README covering setup (getting the Hevy API key, storing it in Vault, creating the AppRole and its policy, pulling from GHCR, Traefik labels and network, and adding the server to Claude), so that I can redeploy or rebuild it months from now.
38. As a developer, I want the whole tool surface tested through an in-memory MCP client against faked Hevy and Vault HTTP APIs, so that I can refactor internals without rewriting tests.
39. As the home-lab owner, I want the Vault login credentials for the container (an AppRole role ID and secret ID) passed in at deploy time and kept out of the image, so that only my server can read the Hevy key.
40. As the home-lab owner, I want to point the server at my internal CA certificate for Vault, so that TLS to `vault.<home-domain>` is verified rather than turned off.
41. As a developer, I want to set `HEVY_API_KEY` directly as a local-development override that skips Vault, so that I can run the server on my laptop without Vault access.
42. As the home-lab owner, I want the image built and pushed to GitHub Container Registry automatically when I push to the main branch or tag a release, so that deploying is just pulling a new image.
43. As the home-lab owner, I want images tagged with the release version, `latest` and the commit SHA, so that I can pin a known-good version or roll back.
44. As the home-lab owner, I want the Compose file on my server to pull the image from GHCR rather than build it locally, so that the server doesn't need the source code or build tools.
45. As a developer, I want CI to run the test suite before it pushes an image, so that a broken build never reaches GHCR.
46. As a Claude user, I want to use the server from Claude on my phone, the web and Desktop, whose connector calls come from Anthropic's cloud, so that I can ask about my training wherever I am.
47. As the home-lab owner, I want clients to sign in through Cloudflare Access with Google, allowing only my email, so that nobody else can read my data even though the hostname is public.
48. As the home-lab owner, I want the server itself to reject any request without a valid Access JWT for an allowed email, so that a wrong Cloudflare or Traefik setting can't expose my data.
49. As the home-lab owner, I want the server to answer only for the hostnames I configure, so that DNS-rebinding attacks and traffic that reaches the container some other way are refused.
50. As the home-lab owner, I want the tunnel's connection to Traefik verified against my own CA, so that no hop carries my data unverified.
51. As the home-lab owner, I want Cloudflare to block `/mcp` traffic from outside the ranges I expect and to rate-limit it, so that a misbehaving client can't use up my Hevy API quota.
52. As the home-lab owner, I want every tool call logged with the caller's verified email, so that the container logs double as an audit trail.

## Implementation Decisions

**Stack**
- Python 3.14, with dependencies managed by `uv` and a lockfile committed.
- FastMCP 4.x (the latest release at the time of writing is 4.1.4), pinned `>=4.1,<5`. Server-initiated sampling and roots were removed in 4.0 and are not needed here.
- `httpx` async client for Hevy calls, and Pydantic models for Hevy response shapes.

**Modules**
- **Config**: reads settings from the environment and validates them at startup. Settings are the Vault settings below, an optional `HEVY_API_KEY` override, `HEVY_BASE_URL` (default `https://api.hevyapp.com`), `HEVY_TIMEOUT_SECONDS`, `LOG_LEVEL`, the bind host and port (default `0.0.0.0:8000` inside the container), the Access settings `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD` and `CF_ACCESS_ALLOWED_EMAILS` (all three or none, or startup stops), and `MCP_ALLOWED_HOSTS` (exact hostnames, comma-separated).
- **Secret source**: resolves the Hevy API key once at startup and holds it as a secret type, so that it never appears in a repr or log.
  - If `HEVY_API_KEY` is set, it is used and Vault is skipped. This is for local development only.
  - Otherwise it reads the key from HashiCorp Vault at `VAULT_ADDR` (required, no default, so that no real hostname is committed). It logs in with AppRole: `VAULT_ROLE_ID` and `VAULT_SECRET_ID`, each readable from a file (a `_FILE` variant) so that Docker secrets work. It then reads a KV v2 secret at a configurable mount and path (defaults `secret` and `hevy-mcp`), taking the field `api_key`.
  - `VAULT_CACERT` points at the internal CA bundle. TLS verification is never turned off.
  - It talks to Vault with `httpx` directly (two calls: AppRole login, then KV read), not with a Vault SDK. This keeps one HTTP stack and lets tests fake Vault through the same injectable transport as Hevy.
  - The key is read once per process. Rotating it in Vault requires restarting the container, which is acceptable for a single-user server.
  - Any Vault failure (unreachable, login denied, missing path or field) stops startup with a message that names the Vault address and path but never any credential.
- **Host check**: ASGI middleware that answers `421` to any Host header outside `MCP_ALLOWED_HOSTS`, before anything else runs. Loopback names are allowed only on `/health`, so the container's health check keeps working. With the setting empty, any host is accepted.
- **Access check**: ASGI middleware that verifies the `Cf-Access-Jwt-Assertion` header on every request except `/health`: an RS256 signature against the team's published keys, the audience, the issuer, expiry, and an email in `CF_ACCESS_ALLOWED_EMAILS` (case-insensitive). Failures get `403`, or `503` if no signing keys can be fetched, and are logged without the token or its claims. Signing keys are cached and refetched once they're an hour old. An unknown key id triggers a refresh at most once a minute, and a failed fetch pauses fetching for five seconds. Without the Access settings the server is unauthenticated and logs a warning at startup. That mode is for local development and tests only. [ADR 0001](docs/adr/0001-cloudflare-access-for-remote-clients.md) has the reasoning.
- **Hevy client**: a thin, deep async module that owns all HTTP concerns: the `api-key` header, base URL, timeouts, retry with jittered backoff on 429, 5xx and timeouts, mapping status codes to domain errors (`HevyAuthError` for 401/403, `HevyNotFound` for 404, `HevyUnavailable` after retries run out), page-size clamping, and lazy multi-page iteration. It accepts an injectable HTTP transport, which is the test seam. It exposes typed read methods only.
- **Analysis**: pure functions that turn lists of workouts and exercise templates into derived statistics (the training summary). They have no I/O.
- **MCP server**: the FastMCP app. It registers the tools, maps domain errors to MCP tool errors with user-friendly messages that contain no secrets, adds a `/health` custom HTTP route, and exposes Streamable HTTP at `/mcp`. Every tool is annotated `readOnlyHint=true` and `openWorldHint=true`. Every tool call is logged at `INFO` with the caller's verified email (or `unauthenticated` without Access) and the tool name. Tokens, the Access header and the API key are never logged, at any level.

**Hevy API facts that shape the design** (from Hevy's OpenAPI document, v0.0.1)
- Authentication is an `api-key` request header. The API is only available to Hevy Pro users. The key is issued at `hevy.com/settings?developer`.
- Hevy warns that the API may change or be abandoned. The client should therefore tolerate unknown fields and treat most fields as optional.
- Maximum page sizes: workouts 10, workout events 10, routines 10, routine folders 10, body measurements 10, exercise templates 100.
- Workouts have no date filter. Range tools page newest first and stop once they pass the start date. Body measurements are handled the same way. Exercise history does take `start_date` and `end_date` natively.
- Hevy stores weights in kg and distances in metres. User info carries the preferred `weight_unit` and `distance_unit`.
- Hevy asks scheduled jobs not to fire exactly on the hour. That doesn't apply here (there's no polling), but nothing in the server should poll.

**Tool surface (all read-only)**

| Tool | Backed by |
|---|---|
| `get_user_info` | `GET /v1/user/info` |
| `list_workouts(page, page_size)` | `GET /v1/workouts` |
| `get_workout(workout_id)` | `GET /v1/workouts/{id}` |
| `get_workout_count()` | `GET /v1/workouts/count` |
| `get_workouts_in_range(start_date, end_date)` | Pages `GET /v1/workouts` lazily |
| `get_training_summary(start_date, end_date)` | Workouts in range plus exercise templates, then Analysis |
| `list_workout_events(since, page, page_size)` | `GET /v1/workouts/events` |
| `get_exercise_history(exercise_template_id, start_date?, end_date?)` | `GET /v1/exercise_history/{id}` |
| `search_exercise_templates(query)` | Lists all templates (100 per page), then a case-insensitive substring match on the title |
| `list_exercise_templates(page, page_size)` / `get_exercise_template(id)` | `GET /v1/exercise_templates[/{id}]` |
| `list_body_measurements(page, page_size)` / `get_body_measurement(date)` | `GET /v1/body_measurements[/{date}]` |
| `get_body_measurements_in_range(start_date, end_date)` | Pages body measurements lazily |
| `list_routines` / `get_routine` / `list_routine_folders` / `get_routine_folder` | The matching `/v1/routines` and `/v1/routine_folders` endpoints |

- Range tools cap how many pages they walk (configurable, with a sensible default such as 50 pages, or 500 workouts). If the cap stops them early, the result says it was truncated.
- Exercise templates are cached in memory with a short TTL, because they change rarely and `search_exercise_templates` and `get_training_summary` both need the full list.
- The training summary covers the workout count, total duration, total volume (sum of weight_kg × reps over working sets, with warm-ups excluded), total sets and reps, per-exercise set counts and top weight, and set counts per primary muscle group.
- Dates are ISO 8601. Date-only inputs are interpreted in the server's configured timezone (`TZ`, default UTC).

**Container and deployment**
- A multi-stage Dockerfile with a `uv` build stage and a `python:3.14-slim` runtime stage. It runs as a non-root user, exposes port 8000 inside the container only, and has a `HEALTHCHECK` that hits `/health`.
- A Compose file defines one service with `restart: unless-stopped` and no `ports:` mapping. The service pulls the image from GHCR rather than building it. The Vault AppRole role ID and secret ID are mounted as Docker secrets, the internal CA bundle is mounted read-only, and the Hevy API key itself is never in the Compose setup. The service attaches to the existing Traefik external network, whose name is set by a variable because Traefik runs in its own Compose project.
- Traefik is configured through labels on the service: `traefik.enable=true`, a router, the entrypoint matching the existing Traefik setup, and a service pointing at port 8000. Response buffering must not break streaming, so no buffering middleware is added.
  - The router's `Host()` rule matches both the public hostname (such as `hevy-mcp.example.com`), which the tunnel sends as the Host header, and the home-network hostname (such as `hevy-mcp.home.example`). Both are placeholders.
  - Its certificate is for the home-network hostname only, issued by an existing Traefik resolver that uses the home CA (for example step-ca). Traefik picks the certificate by TLS server name and the router by Host header, which is what lets the two names differ. The project assumes such a resolver exists and doesn't set one up.
  - The labels that name both hostnames and the resolver go in the gitignored `compose.override.yml`.
- **Image publishing:** a GitHub Actions workflow builds the image and pushes it to `ghcr.io/thedownwardspiral/hevy-mcp`, logging in with the workflow's built-in `GITHUB_TOKEN` (`packages: write` permission).
  - It runs on pushes to the main branch and on `v*` tags.
  - It runs the test suite first and pushes nothing if the tests fail.
  - Tags are the semver version (on tags), `latest` (on main) and `sha-<short sha>`.
  - It builds for `linux/amd64` by default. Add `linux/arm64` if the home server is ARM.
  - The image carries OCI labels linking it back to the source repository.
  - If the GHCR package is private, the home server runs `docker login ghcr.io` once with a personal access token limited to `read:packages`.
- FastMCP 4 supports the sessionless 2026-07-28 protocol revision, and older clients still get sessions. With a single replica, no sticky sessions are needed in Traefik.
- **Exposure:** the only way in from the internet is the Cloudflare Tunnel. Traefik's entrypoints and the Docker host must not be reachable from the internet directly. The Cloudflare side is set up by hand, following the README, and is configured as follows.
  - **Tunnel route:** the public hostname with path `^/mcp$`, so only the MCP endpoint is forwarded and `/health` never leaves the home network. It sends the public hostname as the Host header.
  - **Origin TLS:** verified. The route's Origin Server Name is the home-network hostname and its CA Pool is the home CA's root certificate on the `cloudflared` host. No TLS Verify stays off.
  - **Access application:** covers the whole public hostname. It uses Managed OAuth, with redirect URIs limited to claude.ai's callback plus localhost and loopback clients for Claude Code. It has one Allow policy for my email, and Google (with PKCE) as the only login method. The session lasts a week, and the cookie is HTTP-only and hidden from the App Launcher.
  - **WAF custom rule:** blocks the hostname unless the source is in Anthropic's outbound range (`160.79.104.0/21`) or my own ISP's ranges. This is an extra layer, not authentication, because every Claude user's connector traffic comes from Anthropic's range.
  - **Rate-limiting rule:** on the path `/mcp`, counted per IP, blocking for 10 seconds after 30 requests in 10 seconds. That is what the Free plan allows.
- On the home network, the server is reachable at its home-network hostname, but with Access configured only `/health` answers there without a JWT. LAN clients sign in through the public hostname like everyone else.

## Testing Decisions

- **One seam: the MCP tool interface.** Tests connect FastMCP's in-memory `Client` to the real server object and call tools by name, the same way a real MCP client would. Below that seam everything is real code (tool registration, error mapping, the Hevy client, pagination, retry and analysis) except the network. Hevy's and Vault's HTTP APIs are replaced by a fake HTTP transport (an `httpx.MockTransport`, or a router such as `respx`) that serves canned JSON. Because Vault is reached through the same injectable transport, startup secret loading sits behind the same seam.
- Canned Hevy responses are fixture files modelled on the example payloads in Hevy's OpenAPI document. This keeps fixtures realistic and makes API drift easy to spot. Fixtures must be synthetic: the repository is public, so real workouts or body measurements from my account must never be committed.
- **What makes a good test:** it asserts only on what an MCP client sees, meaning the tool list and its schemas, tool results, and tool errors. It also checks which Hevy requests were made (path, query and the presence of the `api-key` header). It never imports or asserts on internal functions or classes. If an internal refactor breaks a test without changing tool behaviour, the test is wrong.
- **Behaviours to cover:**
  - Every tool is listed and marked read-only.
  - Each tool's happy path.
  - Pagination: page-size clamping, page counts, and range tools stopping at the start date, plus the truncation flag.
  - `search_exercise_templates` matches across several template pages.
  - Training summary figures computed from a known fixture, with warm-ups excluded.
  - 401 produces the auth error message that mentions Hevy Pro, and the key is not in the message.
  - 404 produces a not-found error.
  - 429 and 5xx are retried and then succeed. Retries running out produce an unavailable error.
  - Invalid date input and an end date before the start date are rejected.
  - At startup, the key is read from a faked Vault (AppRole login, then KV v2 read), and Hevy calls carry that key.
  - Each Vault failure (unreachable, login denied, missing path, missing `api_key` field) fails startup with a message that contains no credentials.
  - Setting `HEVY_API_KEY` skips Vault entirely.
- **The HTTP layer is a second seam.** The host and Access checks sit in front of the MCP endpoint, so their tests send HTTP requests to the real ASGI app through `httpx.ASGITransport`, with Cloudflare's signing-key endpoint faked through the same transport as Hevy. They assert only on status codes, response bodies, which tools ran and what was logged. They cover:
  - A valid token can list and call tools. Allowed emails ignore case, and a small clock skew is tolerated.
  - A missing, malformed, unsigned, wrongly signed, expired or not-yet-valid token, a wrong audience or issuer, or a missing or disallowed email, is denied before any tool runs. Denials never echo the token or its claims.
  - Every path except `/health` is guarded.
  - Signing keys are cached, a rotated key is picked up with one refresh, unknown key ids can't force repeated refreshes, and an unreachable keys endpoint fails closed.
  - Partial Access settings stop startup. With no Access settings, requests need no token and a warning is logged.
  - Hosts outside `MCP_ALLOWED_HOSTS` get `421` on every path, before the Access check, while loopback still reaches `/health`.
  - A tool call logs the caller and the tool, and debug logs never contain tokens, header values or the API key.
- No tests call the live Hevy API or the real Vault in CI. GitHub-hosted runners can't reach `vault.<home-domain>` anyway. An optional, manually run live check may exist behind an environment flag.
- Prior art: this is a new repository with no existing tests. Follow FastMCP's documented in-memory client testing pattern with `pytest` and `pytest-asyncio` (or `anyio`).

## Out of Scope

- Any write operations: creating or updating workouts, routines, routine folders, exercise templates or body measurements. Hevy's API supports these, but this server is read-only for now.
- An OAuth server, client registration or token storage in the MCP server itself. Cloudflare Access does that, and the server only checks the JWT Access forwards. ADR 0001 records why FastMCP's OAuth proxy, a static bearer token and a token in the URL were rejected.
- Multiple Hevy accounts or multiple users.
- Persistent storage, a local database, or background sync of Hevy data. The server is stateless apart from a short in-memory template cache.
- Health data from other sources (Apple Health, Garmin and similar). The only source is Hevy.
- Charts or visualisations. The server returns data and summaries, and clients render them.
- Setting up Traefik itself, including the certificate resolver that issues the home-network certificate. The README says what the resolver needs, but the project assumes it exists.
- Automating the Cloudflare setup: the tunnel, the Access application, the identity provider and the WAF and rate-limiting rules. The README documents them, and they're configured by hand.
- Automatic deployment to the home server (for example Watchtower or a webhook). Pulling a new image and restarting the container stays manual.
- Setting up Vault itself: the KV mount, the AppRole, its policy and the internal CA. The README documents these, but the project does not automate them.
- Running the MCP server in k3s. Only Vault lives in k3s; the MCP server is a Docker container behind the standalone Traefik instance. For the same reason, Vault's Kubernetes auth method and the Vault Agent injector don't apply, so the server uses AppRole.

## Further Notes

- Hevy describes its public API as early and subject to change or removal. Keep the Hevy client tolerant of unknown fields and in one place, so that changes only need fixing in one module.
- The Hevy API needs an active Hevy Pro subscription. If Pro lapses, every tool will return the auth error.
- The Traefik hostname, entrypoint name, network name and certificate resolver are environment-specific. They should be variables in the Compose setup, not hard-coded values.
- The container must be able to resolve and reach `vault.<home-domain>`, which is exposed from the k3s cluster, from the Docker network it runs on. `.local` names often depend on mDNS or a home DNS server that containers don't use by default. If resolution fails, add a `dns:` or `extra_hosts:` entry in the Compose file.
- **The repository is public.** Committed files use placeholders (`<home-domain>`, `hevy-mcp.home.example`, `hevy-mcp.example.com`, `<team>.cloudflareaccess.com`, `you@gmail.com`) for every real hostname, email, Vault path and Traefik name. The real values live only in a gitignored `compose.override.yml` and `.env` on the server. `.gitignore` must cover `.env`, `.env.*`, `compose.override.yml` and any `secrets/` directory.
- The repository was created from the `thedownwardspiral/repo-template` template. That gives it gitleaks, CodeQL, dependency review, zizmor and a ruleset that requires a PR to change `main`. Add `pip`/`uv` and `docker` entries to its `dependabot.yml`, and keep `uv.lock` committed so that the dependency scanners can see versions.
- The Vault policy for the AppRole should allow only `read` on the single KV path that holds the Hevy key.
- The Vault KV mount and path, and the AppRole names are placeholders until the repository and Vault entries exist.
- Sources: Hevy OpenAPI document (`https://api.hevyapp.com/docs/`), FastMCP on PyPI (`https://pypi.org/project/fastmcp/`), and FastMCP v4 notes (`https://gofastmcp.com/development/v4-notes`).
