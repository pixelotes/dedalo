# Security Policy

## Supported versions

Only the `main` branch is maintained. Security fixes are not backported to earlier versions.

## Reporting a vulnerability

Do not open a public issue. Use GitHub's private reporting:
**Security → Report a vulnerability** in this repository.

If you can, include:

- Affected version or commit and deployment method (`run.sh`, Docker, systemd).
- Steps to reproduce it or a proof of concept.
- The impact you estimate.

Initial response within 7 days. We will coordinate publication of the advisory once a fix is available.

## Threat model

Dédalo is a self-hosted automation tool that **runs code** with the permissions of the user running it and **does not include authentication**. It is designed to listen on `localhost` or on a trusted private network (Tailscale, VPN, SSH tunnel, reverse proxy with authentication).

Out of scope:

- Code execution by someone who already has access to the interface or the API: this is the intended functionality.
- Deployments exposed to the internet without an authenticating proxy.
- Imported third-party flows that run malicious code.

In scope, among others:

- Code execution or secret disclosure without access to the interface (for example, from another website visited in the browser, from content processed by a flow, or through prompt injection targeting `transform.agent` or `transform.coder`).
- Writing or reading files outside `data/`.
- Exposure of secrets in logs, history or exports.
