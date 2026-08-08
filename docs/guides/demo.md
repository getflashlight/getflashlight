# Deploy the demo

The public demo image includes mocked multi-month data and the documentation site. It
does not need credentials or a writable lake.

```bash
docker run -p 8501:8501 ghcr.io/ychaparala/getflashlight-demo:latest
```

Open `http://localhost:8501`. The image runs with `FLASHLIGHT_DEMO=1`, which disables
the dashboard's connection and BYOK assistant surfaces. It intentionally does not start
the MCP server.

Use the image for a product walkthrough, not for production billing data. For TLS,
authentication, or public exposure, put a reverse proxy in front of the container.
