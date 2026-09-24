# Pinned base image on purpose: this system is meant to keep running
# unattended for years between touches. Bump this deliberately, not
# via an unpinned "latest" tag drifting underneath you.
FROM python:3.12.4-slim-bookworm

# signal-cli needs a JRE; install tini for clean signal handling of the
# container's PID 1 (important for graceful shutdown/restart).
RUN apt-get update && apt-get install -y --no-install-recommends \
    tini \
    curl \
    ca-certificates \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# signal-cli 0.14.x requires a Java 25+ runtime (class file version 69),
# but Debian bookworm's default-jre-headless is Java 17 only. Pull a
# Java 25 JRE from Eclipse Temurin's apt repo instead. Pinned version on
# purpose, same rationale as signal-cli below - bump deliberately.
ARG TEMURIN_JRE_VERSION=25.0.4.0.0+7-0
RUN curl -sL https://packages.adoptium.net/artifactory/api/gpg/key/public | gpg --dearmor -o /usr/share/keyrings/adoptium.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/adoptium.gpg] https://packages.adoptium.net/artifactory/deb bookworm main" > /etc/apt/sources.list.d/adoptium.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends "temurin-25-jre=${TEMURIN_JRE_VERSION}" \
    && rm -rf /var/lib/apt/lists/*

# --- signal-cli (pinned version; check https://github.com/AsamK/signal-cli/releases
#     before bumping) ---
ARG SIGNAL_CLI_VERSION=0.14.8
RUN curl -sL "https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}.tar.gz" \
    | tar -xz -C /opt \
    && ln -s "/opt/signal-cli-${SIGNAL_CLI_VERSION}/bin/signal-cli" /usr/local/bin/signal-cli

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY config ./config

# Runtime data (rotation state, history, signal-cli registration) lives
# in a volume so it survives container recreation — see docker-compose.yml.
RUN mkdir -p /app/data /app/signal-cli-data

ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "app.main"]
