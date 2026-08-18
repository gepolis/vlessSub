#!/bin/sh
set -e

echo "=== Initial certificate issuance ==="

# Stop nginx temporarily so certbot can use standalone mode on port 80
docker compose stop nginx

docker compose run --rm certbot certonly \
    --standalone \
    -d sb.transhata.me \
    --email admin@transhata.me \
    --agree-tos \
    --no-eff-email

# Start nginx with SSL
docker compose up -d nginx

echo "=== Certificate obtained. Nginx started on 443 ==="
