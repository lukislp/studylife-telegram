# uv is taken from Astral's own image (pinned by tag + digest, both kept current by
# Dependabot's docker updates) instead of an unpinned `pip install uv`.
FROM ghcr.io/astral-sh/uv:0.12.18@sha256:3adc3706091ce7c2fe595e669628caedd6d951551b92b258b7e7dbe06d9440bc AS uv

FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

# Pull in Debian's security updates on every build: the digest-pinned base image lags behind
# the security archive (fixed CVEs in the base layer blocked the Trivy CRITICAL gate on
# 2026-09-13) and a rebuild is cheaper than waiting for the next python:3.12-slim digest.
# HTTPS mirror because plain-http port 80 is blocked on some build hosts.
RUN sed -i 's#http://deb.debian.org#https://deb.debian.org#' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get -y --no-install-recommends upgrade \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=uv /uv /uvx /usr/local/bin/

WORKDIR /app

# Install dependencies first so this layer is cached as long as
# pyproject.toml / uv.lock don't change (source changes shouldn't
# trigger a full dependency reinstall). Mirrors studylife-mcp's Dockerfile.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# hatch-vcs derives the package version from git tags, but this image never COPYs .git
# (deliberately, to keep the dependency layer above cacheable across releases) - so
# building the project itself below has no VCS history to read. CI passes the exact
# semantic-release version it already computed as a build-arg (see the docker job in
# ci.yml). Un-suffixed SETUPTOOLS_SCM_PRETEND_VERSION, not the dist-specific
# _FOR_STUDYLIFE_TELEGRAM variant - hatch-vcs's own get_version() call never sets
# dist_name, so the dist-specific form is silently never read (same finding as
# studylife-mcp's/studylife-alexa's Dockerfile).
ARG PACKAGE_VERSION=0.0.0+unknown
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${PACKAGE_VERSION}

COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

# No data directory needed - unlike studylife-alexa/studylife-mcp, this bot holds no
# SQLite store: the single StudyLife API key lives in an env var (login.py writes it to
# a local .env at bootstrap time, never inside the container), and every command handler
# is a stateless read/write against StudyLife's own API.
RUN useradd --create-home --uid 1000 appuser
USER appuser

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

CMD ["uvicorn", "studylife_telegram.main:app", "--host", "0.0.0.0", "--port", "8000"]
