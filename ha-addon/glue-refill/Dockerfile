ARG BUILD_FROM
FROM ${BUILD_FROM}

# HA base images are Alpine-based. Python 3 + the crypto deps the refill worker
# needs (Cognito SRP via pycognito, boto3, cryptography for the encrypted store).
RUN apk add --no-cache python3 py3-pip \
 && python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip

# Only the CLI tooling is needed — not esphome, bleak or keyring (no BLE, no OS
# vault inside the container; secrets come from env + the encrypted /data store).
COPY requirements-addon.txt /tmp/requirements-addon.txt
RUN /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements-addon.txt

# Vendor the repo's tools/ into the image.
COPY tools/ /app/tools/

COPY run.sh /run.sh
RUN chmod +x /run.sh

ENV PATH="/opt/venv/bin:${PATH}"
CMD ["/run.sh"]
