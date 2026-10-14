import os

# Offline tests never export telemetry: with the OTel SDK installed and no collector
# listening, every flush() retries against localhost:4317 and stalls the run.
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
