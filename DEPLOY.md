# Host the reviewer site

This stack needs a Linux server that can run Docker Compose. The two app sites
and reviewer dashboard use separate hostnames. Caddy obtains and renews HTTPS
certificates. The dashboard requires a login. Databases, agent APIs and the
dashboard container remain private; only Caddy publishes ports 80 and 443.

## Before deployment

You need a server with Docker Engine and the Docker Compose plugin, plus three
DNS names whose A/AAAA records point to that server: one each for Stockholm,
Helsinki and the dashboard. Allow inbound TCP 80 and 443 (and UDP 443 for
HTTP/3) in the server firewall. The demo pages are synthetic; do not use this
setup for real user accounts or production data.

## Deploy

On the server:

```sh
git clone --branch feat/public-hosting https://github.com/Fault-Furious/Fault-Furious_Codebase.git
cd Fault-Furious_Codebase
cp .env.production.example .env
```

Create DNS records for all three hostnames and point them at the server. Edit
`.env` with those names, a dashboard username, and a Caddy password hash. Generate
the hash on the server with `docker run --rm caddy:2 caddy hash-password
--plaintext 'your-long-random-password'`. Set unique random values for every
database password; `openssl rand -hex 32` generates safe hex values for connection
strings. Keep `.env` private; it is ignored by Git. Then validate and start:

```sh
docker compose -f docker-compose.yml -f docker-compose.prod.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build --wait
```

Reviewers can open `https://$STOCKHOLM_DOMAIN` and `https://$HELSINKI_DOMAIN`.
The incident dashboard is at `https://$DASHBOARD_DOMAIN` and prompts for the
configured username and password. It can display telemetry and start an
investigation. Public agents use the deterministic mock by default; connecting
Ollama on the server requires separate model setup and enough server memory.
Fault-injection buttons are disabled in the public deployment.
Check service status with:

```sh
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
```

To update, pull the branch and rebuild:

```sh
git pull --ff-only
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build --wait
```

## Access boundaries

Only Caddy is publicly reachable. Do not publish ports 5433 or 9001–9003. The
public dashboard is protected by Caddy authentication and has no Docker socket
or repository mount, so it cannot trigger host-level fault scripts. Keep access
credentials private and share them only with reviewers.
