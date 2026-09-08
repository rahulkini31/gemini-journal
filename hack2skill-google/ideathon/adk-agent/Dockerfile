# Documented for future use — not built or deployed as part of this
# prototype's delivery (see README.md's "Scope and status" section: this
# branch does not touch the deployed Cloud Run service). Not empirically
# verified with a real `docker build` (no Docker available in the session
# that wrote this) — the layout below was derived by tracing through
# app/main.py::_read_firebase_config()'s actual path arithmetic
# (Path(__file__).resolve().parents[2]) rather than assumed, since an
# earlier version of this file had the build context and destination path
# for firebase-applet-config.json silently wrong (it lives at the repo
# root, not inside adk-agent/, and parents[2] needs the same 3-level
# nesting depth this file's own directory has in the real repo).
#
# Build from the REPO ROOT (not adk-agent/) as the context, so both
# adk-agent/ and the root-level firebase-applet-config.json are reachable:
#   docker build -f adk-agent/Dockerfile -t secure-journal-adk-agent .
FROM python:3.12-slim

WORKDIR /service
COPY adk-agent/pyproject.toml adk-agent/pyproject.toml
COPY adk-agent/app adk-agent/app
COPY firebase-applet-config.json firebase-applet-config.json
RUN pip install --no-cache-dir ./adk-agent

# Preserves the same repo-root/adk-agent/app/main.py nesting depth the
# local dev layout has, so _read_firebase_config()'s parents[2] lands on
# /service (where firebase-applet-config.json was just placed) exactly the
# way it lands on the real repo root in local dev.
WORKDIR /service/adk-agent

ENV PORT=8080
EXPOSE 8080
CMD ["python", "-m", "app.main"]
