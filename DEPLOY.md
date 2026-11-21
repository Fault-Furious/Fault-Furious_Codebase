# Host the reviewer site

This stack needs a Linux server that can run Docker Compose. The two app sites
are published on separate hostnames. Caddy obtains and renews HTTPS certificates.
The database and agent API ports remain private; only Caddy publishes ports 80
and 443.

## Before deployment

You need a server with Docker Engine and the Docker Compose plugin, plus two DNS
names whose A/AAAA records point to that server. Allow inbound TCP 80 and 443
(and UDP 443 for HTTP/3) in the server firewall. The demo pages are synthetic;
do not use this setup for real user accounts or production data.

## Deploy

On the server:

```sh
git clone --branch integration-working-app https://github.com/Fault-Furious/Fault-Furious_Codebase.git
cd Fault-Furious_Codebase
cp .env.production.example .env
```

Edit `.env` with the two real hostnames and unique random values for every
password. Hex values avoid special-character issues in database connection
strings; for example, generate each with `openssl rand -hex 32`. Keep `.env`
private; it is ignored by Git. Then validate and start the stack:

```sh
docker compose -f docker-compose.yml -f docker-compose.prod.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build --wait
```

Reviewers can open `https://$STOCKHOLM_DOMAIN` and `https://$HELSINKI_DOMAIN`.
Check service status with:

```sh
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
```

To update, pull the branch and rebuild:

```sh
git pull --ff-only
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build --wait
```

## Current reviewer experience

This exposes the two sample forum sites. It does not provide an incident
dashboard: diagnosis results are currently JSON from the private agent APIs.
Do not publish ports 9001–9003 for review. If reviewers need to see diagnoses,
the project needs a small authenticated reviewer page or a protected report
endpoint before deployment.
