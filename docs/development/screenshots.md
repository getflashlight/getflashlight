# Capture dashboard screenshots

The setup and sync guides use real Flashlight UI screenshots. Capture them from the
deterministic sample container, never from a production lake or a browser containing real
credentials.

## Run the screenshot dashboard

From the repository root, build and start the disposable container:

```bash
docker build -f Dockerfile.docs-screenshots -t flashlight-docs-screenshots .
docker run --rm --name flashlight-docs-screenshots -p 8501:8501 flashlight-docs-screenshots
```

Open `http://127.0.0.1:8501`. The container creates an isolated `/data` lake, seeds the
deterministic sample data, and starts the dashboard. It has no cloud credentials and does
not read the host's Flashlight home.

Stop the container with `Ctrl+C` when finished. Because it is disposable, the sample lake
is removed with the container.

## Required images

Capture only stable user decision points. Save approved images as JPEG under
`docs/assets/screenshots/` with these names:

| File | State to capture | What the reader should notice |
| --- | --- | --- |
| `connections-empty.jpg` | Connections page before a source is added | **Add connection** is the entry point. |
| `add-connection.jpg` | Connection dialog with a provider selected | Source type, identity fields, and **Save**. |
| `add-databricks-connection.jpg` | Databricks form | Workspace host, optional SQL warehouse, and secure token field. |
| `add-redshift-connection.jpg` | Redshift form | Connection-method tabs and **Test connection**. |
| `add-snowflake-connection.jpg` | Snowflake form | Account / role choices and optional authentication details. |
| `connection-saved.jpg` | Saved documentation fixture | How a configured source appears before its first real sync. |
| `sync-progress.jpg` | Active sync dialog | Progress, output, and where to open the log. |
| `sync-history.jpg` | Completed sync history | Status, record count, and log access. |
| `provider-overview.jpg` | Populated provider overview | The first place to verify scope and totals. |
| `assistant-configuration.jpg` | Assistant settings dialog | Provider/model selection and keychain-backed API-key field. |
| `mcp-server.jpg` | MCP server page before start | Start control, endpoint, client configuration, and tools. |

Do not show actual access keys, passwords, tokens, account identifiers, organization names,
resource names, query text, or customer cost data. Use the sample container whenever
possible. If a connector form must be captured, use clearly fictitious values and leave any
secret input empty.

## Add a screenshot to a guide

Keep one screenshot close to the action it supports. Use descriptive alt text and a short
caption that tells the reader what to look for; do not add a screenshot merely as decoration.

```md
![The Connections page with Add connection highlighted](../assets/screenshots/connections-empty.jpg)

*Add each billing or telemetry source from **Connections**.*
```
