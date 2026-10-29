# Basis-Image per Digest gepinnt (Review 29.10.2026, A4) - wie alle anderen Images.
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159

WORKDIR /app

# iproute2 (fuer "ip"): noetig fuer _local_host_subnets() in main.py
# (Router-Geraete-Erkennung, Nutzerwunsch 2026-08-31) - der Container laeuft
# mit network_mode:host und sieht dadurch die echten Host-Netzwerk-
# interfaces, das schlanke python-slim-Basisimage bringt "ip" aber nicht
# von Haus aus mit.
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY main.py settings.py pip_logic.py guard_logic.py net_logic.py overlay_logic.py stream_key.py ./
COPY belabox-pip ./belabox-pip
COPY belabox-guard ./belabox-guard
COPY belabox-net ./belabox-net
COPY static ./static

EXPOSE 8300

CMD ["uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8300"]
