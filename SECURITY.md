# Security Policy

## Reporting a vulnerability

If you find a security issue in this project (for example secret leakage, path traversal, or unsafe downloads), please open a **private** security advisory on GitHub if available, or contact the maintainer without posting secrets publicly.

Please include:

- Steps to reproduce
- Impact
- Suggested fix (optional)

Do **not** open a public issue that includes API keys or other credentials.

## What this tool does with credentials

- Reads `PEXELS_API_KEY` from a local `.env` file (or environment variables)
- Sends it only as the `Authorization` header to `https://api.pexels.com`
- Never writes the key into `config.yaml`, `LIBRARY.md`, or download state
- Never prints the key to the console



## Hardening already in place

- `.env` is gitignored (plus common secret filename patterns)
- Category folder names are validated (no `../` path traversal)
- `output_dir` and `state_file` must resolve inside the project directory
- Video downloads must be HTTPS and from allowlisted Pexels / Vimeo CDN hosts
- Redirect targets are re-checked before writing files



## Maintainer checklist before going public

1. Confirm `.env` is **not** staged or committed (`git status`)
2. Confirm no real API key appears in README, issues, or commit history
3. If a key was ever committed, **revoke it** in the Pexels dashboard and generate a new one
4. Prefer GitHub secret scanning / push protection on the repository



## Scope notes

This is a local CLI downloader. It does not run a server, accept remote user input, or store third-party credentials beyond your own Pexels key.