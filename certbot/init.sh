#!/bin/bash
set -e

echo "=== Initial certificate issuance ==="

# Start nginx with HTTP-only config for ACME challenge
docker compose up -d nginx

echo "### Waiting for nginx to be ready..."
sleep 2

# Get certificate using webroot mode (nginx already serves ACME challenges)
docker compose run --rm certbot certonly \
    --webroot \
    -w /var/www/certbot \
    -d sb.transhata.me \
    --email admin@transhata.me \
    --agree-tos \
    --no-eff-email

# Switch nginx config to SSL
cp nginx/ssl.conf nginx/default.conf

# Reload nginx to pick up SSL config
docker compose exec nginx nginx -s reload

echo "=== Certificate obtained. Service is available at https://sb.transhata.me ==="
