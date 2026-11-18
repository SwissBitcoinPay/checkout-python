# Security

This repository is the Python SDK with which a shop's server takes Lightning and on-chain payments
for a sattle.me merchant. A flaw here could let an attacker be paid instead of the merchant, mark
an order paid that is not, or learn a key: please report it **privately**, never in a public
issue, pull request or discussion.

## How to report

- On GitHub: **Security** tab of this repository → **Report a vulnerability** (private
  vulnerability reporting).
- Or by email: security@sattle.me, subject starting with `[security] checkout-python`.

Please include what you found, the module or the function concerned, the version of the package
(`sattleme_checkout.__version__`), and, when you can, the steps or the input that show it.
Do not test against real merchants' funds or keys.

## What is in scope

- This package: anything that, used as its README says, accepts a wrong invoice, a wrong payment
  or a forged record, shows an invoice that was not declared, leaks the integration key or a key
  derived from it, or departs from the specification it implements in a way that could cost a
  merchant money.
- A flaw of the specification itself (a rule that, followed as written, is unsafe) goes to
  [checkout-spec](https://github.com/sattleme/checkout-spec/blob/main/SECURITY.md), through
  its own private channels.
- A flaw of a dependency goes to that dependency's maintainers; tell us too, so that we pin a
  fixed version.

We will acknowledge your report, keep you informed, and credit you when the fix is published,
unless you prefer otherwise.
