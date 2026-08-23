# Video Factory — webapp container.
#
# Stdlib-only Python, so the only thing this image needs beyond the base
# is ffmpeg (post-production: subtitle burn-in, and probing durations for
# every post-prod quote). No pip install step, no requirements.txt — there
# is nothing to install; see webapp/README.md and scripts/factory.py for
# why that constraint is deliberate across this whole project.

FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

RUN useradd --create-home --uid 1000 videofactory \
    && mkdir -p /app/work \
    && chown -R videofactory:videofactory /app
USER videofactory

# work/ (generated media, job history, ledger) should be a mounted volume
# in production so it survives a container restart/redeploy.
VOLUME ["/app/work"]

EXPOSE 8000

# Binds 0.0.0.0 inside the container by design — the container's own
# network namespace is not the public internet. Put a reverse proxy (with
# TLS) in front and/or set WEBAPP_BASIC_AUTH_USER/PASS before exposing the
# host port publicly; see DEPLOY.md.
#
# --port is deliberately omitted: webapp/server.py defaults it from the
# PORT env var (falling back to 8000 if unset), which is what lets this
# same image work unmodified on PaaS platforms (Railway, etc.) that
# assign a container's port at runtime.
CMD ["python3", "webapp/server.py", "--host", "0.0.0.0"]
