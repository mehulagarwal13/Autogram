# syntax=docker/dockerfile:1

# --- Stage 1: build the Vite SPA -----------------------------------------------
FROM node:20-alpine AS build
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# VITE_API_URL is baked into the bundle at build time (Vite inlines import.meta.env
# at build, not runtime). Railway automatically exposes a service's variables as
# Docker build args, so declaring the ARG is enough — set VITE_API_URL on the
# Railway service to the backend's public URL, e.g.
#   VITE_API_URL=https://${{backend.RAILWAY_PUBLIC_DOMAIN}}
ARG VITE_API_URL=""
ENV VITE_API_URL=$VITE_API_URL
RUN npm run build

# --- Stage 2: serve the static build with nginx ------------------------------
FROM nginx:1.27-alpine

# Railway assigns a dynamic $PORT. The nginx image renders /etc/nginx/templates/
# *.template through envsubst on boot; the FILTER keeps it from touching nginx's
# own $uri / $host runtime variables.
ENV PORT=8080
ENV NGINX_ENVSUBST_FILTER="^PORT$"

COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 8080
