---
status: accepted
---

# Cloudflare Access authenticates remote clients, and the server checks its JWT on every route

The original spec kept the server unauthenticated and off the internet. To use it from Claude on a phone, it now has to be reachable from Anthropic's cloud, because that's where Claude's connector calls come from. We put it behind a Cloudflare Tunnel, with Cloudflare Access (Managed OAuth) as the OAuth authorization server Claude signs in through. The server holds no OAuth state. It checks the JWT that Access forwards in `Cf-Access-Jwt-Assertion` on every request except `/health` (signature against the team's keys, audience, issuer, expiry, and an allow-listed email), so a misconfigured tunnel, Access application or route fails closed instead of exposing the data. There's no exemption for the home-network (Traefik) route: once Access is configured, LAN clients sign in through the tunnel hostname like everyone else.

## Considered options

- **FastMCP's built-in OAuth proxy (GitHub or Google as the identity provider).** Rejected: the server would have to own client registration, token storage, a signing key and refresh-token rotation. That's a lot of security-sensitive state for a single-user server whose code is public.
- **A static bearer token in a request header.** Rejected: Claude only offers this in beta to a few organizations, and only an organization Owner can set it.
- **A secret token in the URL.** Rejected: the MCP spec forbids access tokens in the query string, and URLs end up in logs.
- **Trusting Access and not checking the JWT in the server.** Rejected: one wrong Cloudflare or Traefik setting would then expose the server to anyone.

## Consequences

- Limiting `/mcp` to Anthropic's outbound range (`160.79.104.0/21`) with a Cloudflare WAF rule is an extra layer, not authentication: every Claude user's connector traffic comes from that range. The IP condition must not go in the Access policy, because sign-in happens in the phone's browser, from the phone's IP.
- Without the `CF_ACCESS_*` settings the server is unauthenticated, as before. That mode is for local development and tests only, and the server logs a warning at startup.
- Signing keys are fetched lazily and cached. An unknown key id triggers a refresh at most once a minute, so Cloudflare's key rotation is picked up without a restart, and forged key ids can't flood the certs endpoint.
