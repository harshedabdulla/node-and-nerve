# Contributing

Thanks for your interest. This is a small project; issues and focused pull requests are welcome.

## Setup

```sh
docker compose up -d falkordb
uv sync
uv run pytest            # unit + integration
uv run ruff format src tests && uv run ruff check src tests && uv run mypy
```

## Guidelines

- **Queries**: add new Cypher only as a named `Query` in `src/company_brain/queries.py`. Pass every value as a parameter, bound every variable-length pattern, add a `LIMIT` to open-ended reads, and add an integration test that runs it against FalkorDB. `tests/test_queries.py` must keep passing.
- **Schema changes**: new kinds, properties or relationship types go in `schema.py` with endpoint rules. Update `docs/graph-model.md`.
- **No raw Cypher from callers**, ever. Not from the API and not from an LLM.
- **Honest output**: findings are rendered from graph rows. If something is unknown, report it in `missing_information`, don't infer it.
- **Data**: only synthetic or public data in fixtures. No real names, emails, credentials or customer data.
- Keep dependencies minimal and justify new ones in the pull request.
