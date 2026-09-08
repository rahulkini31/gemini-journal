# Documented for future use only — not built or deployed as part of this
# prototype's delivery. See README.md's "Scope and status" section: this
# branch does not touch the deployed Cloud Run service.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY app ./app
COPY firebase-applet-config.json ../firebase-applet-config.json
RUN pip install --no-cache-dir .

ENV PORT=8080
EXPOSE 8080
CMD ["python", "-m", "app.main"]
