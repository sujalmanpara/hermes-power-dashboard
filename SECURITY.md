# Security Policy

Hermes PowerDashboard is intended to run as a local operator console for Hermes Agent.

## Reporting a vulnerability

Please open a private security advisory on GitHub or contact the maintainer directly.

## Sensitive data

Do not commit:

- `oauth-creds.json`
- `.env` files
- local Hermes databases
- API tokens
- Cloudflare tunnel logs
- generated auth/profile files

Before exposing the dashboard outside localhost, put it behind your own authentication and network controls.
