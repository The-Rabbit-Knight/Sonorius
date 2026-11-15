# Local Voice Engine Setup (Kokoro-82M)

Sonorius uses the **Kokoro-82M** neural voice engine via its local `/v1/audio/speech` endpoint. You can run the Kokoro container on the same machine as Sonorius (recommended) or on another machine on your local network.

---

## Option 1: Podman or Docker (CPU)

The fastest way to run Kokoro locally on Linux, macOS, or Windows is with the official pre-built CPU container:

```bash
# Using Podman:
podman run -d --name kokoro-tts -p 8880:8880 ghcr.io/remsky/kokoro-fastapi-cpu:latest

# Or using Docker:
docker run -d \
  --name kokoro-tts \
  -p 8880:8880 \
  --restart unless-stopped \
  ghcr.io/remsky/kokoro-fastapi-cpu:latest
```

Once running, Kokoro listens at `http://localhost:8880/v1/audio/speech` (Sonorius connects to this address automatically).

**Next time you restart your computer:** Since the `kokoro-tts` container is already created, simply start it again instead of running `podman run` / `docker run` a second time:
```bash
podman start kokoro-tts   # or: docker start kokoro-tts
```

*(For NVIDIA GPU containers or native Python setup without containers, follow the upstream instructions in the official [remsky/Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI) repository.)*

---

## Connecting to a Remote / LAN TTS Server

By default, Sonorius sets `KOKORO_TTS_URL="http://localhost:8880/v1/audio/speech"`.
- If you point `KOKORO_TTS_URL` in `.env` to another machine on your LAN or network, Sonorius **requires `https://`** by default (`KOKORO_CA_BUNDLE` can be set in `.env` for self-signed certificates).
- Plain `http://` to a non-localhost IP is blocked unless you explicitly set `ALLOW_INSECURE_HTTP_TTS="true"` in `.env` (for example, over an encrypted tunnel).

---

## Verifying the Endpoint

Test that your TTS service is responding before launching Sonorius:

```bash
curl -X POST http://localhost:8880/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{
    "model": "kokoro",
    "input": "Testing local audio synthesis for Sonorius.",
    "voice": "af_bella",
    "response_format": "mp3"
  }' \
  --output test_speech.mp3
```

Open `test_speech.mp3` in any media player (or run `mpv test_speech.mp3` on Linux / `afplay test_speech.mp3` on macOS) to confirm the audio plays cleanly.

---

## Available Voice Profiles in Sonorius

Sonorius exposes **9 solo voices** and **5 blended voice presets** in the UI (`VOICE_OPTIONS` in `app.py`):

### Solo Voices
| Voice Key | Description | Default Role |
| :--- | :--- | :--- |
| `af_sarah` | Sarah - Female (American, Professional & Clear) | **Interviewer Voice (Default)** |
| `am_michael` | Michael - Male (American, Friendly & Conversational) | **Candidate Voice (Default)** |
| `af_heart` | Heart - Female (American, Flagship Grade-A) | - |
| `af_bella` | Bella - Female (American, Warm & Natural) | - |
| `af_nicole` | Nicole - Female (American, Crisp & Direct) | - |
| `am_fenrir` | Fenrir - Male (American, Deep & Authoritative) | - |
| `am_adam` | Adam - Male (American, Deep & Steady) | - |
| `bf_emma` | Emma - Female (British, Refined & Polished) | - |
| `bm_george` | George - Male (British, Authoritative & Formal) | - |

### Blended Voice Presets
| Voice Key | Description |
| :--- | :--- |
| `af_heart(0.6)+af_bella(0.4)` | Executive Blend: Heart + Bella (Natural & Warm) |
| `af_heart(0.6)+af_sarah(0.4)` | Confident Blend: Heart + Sarah (Clear & Assertive) |
| `am_fenrir(0.6)+am_michael(0.4)` | Anchor Blend: Fenrir + Michael (Balanced Deep Male) |
| `af_bella(0.5)+af_nicole(0.5)` | Dynamic Blend: Bella + Nicole (Conversational & Sharp) |
| `af_heart(0.5)+am_michael(0.5)` | Duo Harmony: Heart + Michael (Hybrid Cadence) |

You can switch voices in the UI under **Play Session** → **Preferences** (separate selectors for Interviewer and Candidate voices) and click **Save Preferences** to persist your choices.
