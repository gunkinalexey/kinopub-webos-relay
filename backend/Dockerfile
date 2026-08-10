FROM python:3.12-slim

# FFmpeg is only needed by one feature - the server-side remux that switches
# an audio track when the stream itself carries just one (the last rung of the
# player's audio ladder; see /audio-hls/jobs). It is also by far the largest
# thing in this image and the slowest step of a no-cache rebuild, so it can be
# left out entirely: set WITH_FFMPEG=0 in .env. Everything else - catalogue,
# playback, direct/relay/HLS, subtitles, the first three audio-switching
# tiers - does not touch it.
ARG WITH_FFMPEG=1

WORKDIR /app
COPY requirements.txt .
# apt is not even contacted when it is off, which is what makes the build both
# smaller and quicker rather than just skipping one package.
RUN if [ "$WITH_FFMPEG" = "1" ]; then \
        apt-get update \
        && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ffmpeg \
        && rm -rf /var/lib/apt/lists/*; \
    fi \
    && pip install --no-cache-dir -r requirements.txt
COPY app ./app

# Baked in from the build arg so the running container reports the truth about
# the image it actually is, even if compose forgets to pass the variable
# through. The app still verifies with `shutil.which` on top of this - an
# env var claiming a binary that is not there would be worse than no flag.
ENV WITH_FFMPEG=${WITH_FFMPEG}

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
