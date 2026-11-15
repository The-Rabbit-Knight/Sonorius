#!/usr/bin/env bash
# Startup script for Sonorius (Local Voice-Assisted Q&A & Interview Prep)
#
# Usage:
#   ./run_app.sh              # Default: http://localhost:8501 (loopback-only; uses HTTPS automatically if trusted mkcert or custom certs exist)
#   ./run_app.sh --https      # Force HTTPS on https://localhost:8501 (uses mkcert if installed, otherwise openssl self-signed leaf cert)
#   ./run_app.sh --http       # Force plain HTTP on http://localhost:8501 (loopback only)
#   ./run_app.sh --lan        # Listen on 0.0.0.0:8501 (REQUIRES HTTPS; refuses plain HTTP over LAN)

set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$DIR"

# Load .env if present so SSL_CERT_FILE / SSL_KEY_FILE are respected
if [ -f "$DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$DIR/.env"
    set +a
fi

MODE="auto"
BIND_ADDR="localhost"
PORT="${SONORIUS_PORT:-8501}"

for arg in "$@"; do
    case "$arg" in
        --http)
            MODE="http"
            ;;
        --https)
            MODE="https"
            ;;
        --lan)
            BIND_ADDR="0.0.0.0"
            MODE="https"
            export ALLOW_LAN_ACCESS="true"
            ;;
    esac
done

# Find Streamlit: local .venv → parent .venv → system PATH
STREAMLIT_BIN=""
for candidate in "$DIR/.venv/bin/streamlit" "$DIR/../.venv/bin/streamlit"; do
    if [ -x "$candidate" ]; then
        STREAMLIT_BIN="$candidate"
        break
    fi
done
if [ -z "$STREAMLIT_BIN" ] && command -v streamlit >/dev/null 2>&1; then
    STREAMLIT_BIN="$(command -v streamlit)"
fi

if [ -z "$STREAMLIT_BIN" ]; then
    echo ""
    echo "ERROR: Streamlit is not installed."
    echo "Set up a virtual environment first:"
    echo ""
    echo "    python3 -m venv .venv"
    echo "    source .venv/bin/activate"
    echo "    pip install -r requirements.txt"
    echo ""
    echo "Then run ./run_app.sh again."
    exit 1
fi

# ---------------------------------------------------------
# Transport Security Setup
# - On localhost, browser WebSockets (wss://) silently fail on untrusted
#   self-signed certs in Safari/Brave/Firefox, causing a blank screen.
#   So 'auto' mode uses HTTPS automatically ONLY when a browser-trusted
#   cert (custom SSL_CERT_FILE or mkcert) is available; otherwise it uses
#   loopback-isolated http://localhost:8501.
# - Passing --https or --lan forces HTTPS (generating a proper non-CA leaf
#   certificate via openssl if mkcert/custom certs are not present).
# ---------------------------------------------------------
CERT_DIR="$DIR/.certs"
CERT_FILE="${SSL_CERT_FILE:-$CERT_DIR/localhost.crt}"
KEY_FILE="${SSL_KEY_FILE:-$CERT_DIR/localhost.key}"
USE_HTTPS="false"

generate_leaf_cert() {
    local allow_openssl="${1:-true}"
    mkdir -p "$CERT_DIR"
    chmod 700 "$CERT_DIR"
    if command -v mkcert >/dev/null 2>&1; then
        mkcert -cert-file "$CERT_FILE" -key-file "$KEY_FILE" localhost 127.0.0.1 ::1 >/dev/null 2>&1 && return 0
    fi
    if [ "$allow_openssl" = "true" ] && command -v openssl >/dev/null 2>&1; then
        openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
            -keyout "$KEY_FILE" -out "$CERT_FILE" \
            -subj "/CN=localhost/O=Sonorius Local" \
            -addext "basicConstraints=critical,CA:FALSE" \
            -addext "keyUsage=critical,digitalSignature,keyEncipherment" \
            -addext "extendedKeyUsage=serverAuth" \
            -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1" >/dev/null 2>&1 && return 0
    fi
    return 1
}

if [ "$MODE" = "https" ]; then
    if [ -f "$CERT_FILE" ] && [ -f "$KEY_FILE" ]; then
        USE_HTTPS="true"
    elif generate_leaf_cert; then
        chmod 600 "$CERT_FILE" "$KEY_FILE" 2>/dev/null || true
        USE_HTTPS="true"
    fi
elif [ "$MODE" = "auto" ]; then
    if [ -n "${SSL_CERT_FILE:-}" ] && [ -f "$SSL_CERT_FILE" ] && [ -n "${SSL_KEY_FILE:-}" ] && [ -f "$SSL_KEY_FILE" ]; then
        USE_HTTPS="true"
    elif command -v mkcert >/dev/null 2>&1; then
        if generate_leaf_cert "false"; then
            chmod 600 "$CERT_FILE" "$KEY_FILE" 2>/dev/null || true
            USE_HTTPS="true"
        fi
    fi
fi

# Never allow binding to 0.0.0.0 (--lan) over plain HTTP
if [ "$BIND_ADDR" != "localhost" ] && [ "$BIND_ADDR" != "127.0.0.1" ] && [ "$USE_HTTPS" != "true" ]; then
    echo "ERROR: Refusing to expose Sonorius on $BIND_ADDR over unencrypted HTTP."
    echo "Install mkcert/openssl or provide SSL_CERT_FILE and SSL_KEY_FILE in .env to use --lan."
    exit 1
fi

echo "=============================================="
echo "          Starting Sonorius Platform          "
echo "=============================================="
echo "Directory    : $DIR"
echo "Streamlit    : $STREAMLIT_BIN"
if [ "$USE_HTTPS" = "true" ] && [ "$BIND_ADDR" = "0.0.0.0" ]; then
    echo "Transport    : Dual HTTP (localhost) + HTTPS ($BIND_ADDR)"
    echo "Local Access : http://localhost:${PORT}"
    echo "LAN Access   : https://<your-ip>:${PORT}"
elif [ "$USE_HTTPS" = "true" ]; then
    echo "Transport    : HTTP + HTTPS (bind: $BIND_ADDR, this computer only)"
    echo "Access App   : https://localhost:${PORT}  (or http://localhost:${PORT})"
else
    echo "Transport    : HTTP (bind: $BIND_ADDR)"
    echo "Access App   : http://localhost:${PORT}"
fi
echo "=============================================="

if [ "$USE_HTTPS" != "true" ]; then
    exec "$STREAMLIT_BIN" run app.py \
      --server.address "$BIND_ADDR" \
      --server.port "$PORT"
fi

PYTHON_BIN="$(dirname "$STREAMLIT_BIN")/python"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3)"
fi

exec "$PYTHON_BIN" "$DIR/tls_proxy.py" "$STREAMLIT_BIN" "$BIND_ADDR" "$PORT" "$CERT_FILE" "$KEY_FILE"
