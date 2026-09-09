FROM python:3.12-slim

WORKDIR /app

# iproute2 (fuer "ip"): noetig fuer _local_host_subnets() in main.py
# (Router-Geraete-Erkennung, Nutzerwunsch 2026-08-31) - der Container laeuft
# mit network_mode:host und sieht dadurch die echten Host-Netzwerk-
# interfaces, das schlanke python-slim-Basisimage bringt "ip" aber nicht
# von Haus aus mit.
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY static ./static

EXPOSE 8300

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8300"]
