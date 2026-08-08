# Documentation guide

Documentation is a product surface. Update it whenever a user-visible command,
configuration field, connector behavior, metric, data contract, or security posture
changes.

## Writing standards

- Lead with the user outcome, then show a minimal working example.
- Keep one page focused on one task or concept; link rather than duplicate rules.
- State prerequisites, permissions, scope boundaries, and failure behavior explicitly.
- Use exact command names and configuration keys in code formatting.
- Label planned functionality as planned—never write it as available.
- For cost claims, name the metric, grain, and qualifications that make it valid.

## Documentation structure

Use **Get started** for a new user's first successful outcome, **User guide** for common
operational tasks, **Concepts** for durable mental models, **Connectors** for source
runbooks, and **Reference** for exhaustive schemas/options. Keep implementation proposals
under design decisions; they are not user instructions until implemented.

## Validate locally

```bash
uv run mkdocs build --strict
```

The strict build catches broken internal links and navigation references. Review rendered
pages as well: code blocks, tables, admonitions, and mobile-width navigation should remain
readable.

## Keep reference material accurate

The CLI reference must agree with `flashlight --help`; configuration docs must agree with
the Pydantic models and generated `connections.yml`; MCP docs must agree with the server's
tool definitions. When adding a connector, update its runbook and the support matrix in
the same pull request.
