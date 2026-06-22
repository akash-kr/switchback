# Security Policy

## Reporting a vulnerability

If you discover a security vulnerability in switchback, please report it
privately rather than opening a public issue.

- Use GitHub's [private vulnerability reporting](https://github.com/akash-kr/switchback/security/advisories/new), or
- Email **akash@theaklabs.com** with the details.

Please include steps to reproduce, affected versions, and any relevant logs.
We aim to acknowledge reports within a few days.

## Scope & responsible use

switchback fetches and normalizes web content through a cascade of HTTP, stealth
browser, and paid-API tiers. The stealth / anti-bot tiers exist to handle
legitimate access friction on public pages — **not** to evade access controls,
paywalls, or authentication you are not authorized to bypass. Misuse of this
software against systems you do not own or have permission to access is outside
the scope of this project and is the responsibility of the user. See the
"Responsible use" section of the [README](README.md).

## Supported versions

This project is pre-1.0; security fixes are applied to the latest released
version on a best-effort basis.
