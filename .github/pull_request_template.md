## What

<!-- What this adds or fixes. For a log source: the product, its Devo tables, and what the server can now do with them. -->

## How it was tested

<!-- Offline tests, and any read-only run against a Devo domain (short window, aggregated queries). -->

## Checklist

- [ ] The title follows [Conventional Commits](https://www.conventionalcommits.org) (`feat:`, `fix:`, `docs:`…).
- [ ] No real data from my Devo domain: placeholders only (`jsmith`, `example.com`, `HOST01`, `10.1.2.3`, `203.0.113.0/24`).
- [ ] Offline tests pass (`.venv/bin/python -m pytest`), and a new tool has a `FakeEngine` test.
- [ ] Client values reach the engine as one `--flag=value` token, with positionals after `--`.
- [ ] Engine changes (`src/devo_mcp/engine/`) are also in, or proposed to, [secops-skills](https://github.com/robemmerson/secops-skills).
- [ ] The README is updated if a tool, setting or behaviour changed.
