# Security advisory intake and response targets

This page is the normative process for reporting, triaging, and disclosing a
vulnerability in this repository. It complements
[the security policy](../SECURITY.md) and [the security model](security.md).

## Reporting

Report privately. Never open a public issue, pull request, or commit that
describes an unfixed vulnerability or contains working exploit code.

Preferred channels, in order:

1. GitHub private vulnerability reporting: open the repository's **Security**
   tab and choose **Report a vulnerability**. This creates a private advisory
   visible only to the maintainers.
2. A draft GitHub Security Advisory: **Security** -> **Advisories** ->
   **New draft security advisory**.
3. A private message to the maintainer (`Ayinkx`) on GitHub if neither feature
   is available.

Include the affected module or endpoint, the version or commit, the impact, a
minimal reproduction, and any suggested fix. Do not include real secrets or
third-party personal data. If a credential has already been exposed publicly,
say so immediately so it can be rotated; treat it as compromised even after
the post is removed.

## Acknowledgement and triage

Maintainers aim to acknowledge every private report within **2 business days**
for a suspected **critical** issue and **5 business days** for a suspected
**high** issue. Acknowledgement is a human reply confirming the report was
received and read, not a fix.

Triage then classifies the report and records a remediation plan:

| Severity | What qualifies | Initial assessment and fix plan | Fix target |
| -------- | -------------- | ------------------------------- | ---------- |
| Critical | Remote code execution, authentication or authorization bypass, secret or cross-tenant data exposure, or a supply-chain compromise with a plausible path to exploitation. | **5 business days** | **30 calendar days** |
| High | A serious but narrower confidentiality, integrity, or availability impact, or a critical-class issue that needs an upstream dependency fix first. | **10 business days** | **90 calendar days** |

Medium and low findings are triaged on a best-effort basis and scheduled with
normal maintenance; this page deliberately makes no calendar commitment for
them. Severity is decided by the maintainers after triage, using the impact and
exploitability of this deployment rather than the reporter's label.

These targets are commitments the maintainers make for this repository. They
are not a warranty, a bug bounty, or a promise of a specific fix date when a
third party must release the correction.

## Disclosure

- The advisory stays private until a fix is available or the coordinated
  disclosure date agreed with the reporter arrives.
- Maintainers publish a GitHub Security Advisory with affected versions, impact,
  fixed commit or release, and any required operator action (for example,
  rotating a secret or re-running a migration).
- Credit is given to the reporter in the advisory unless they ask to remain
  anonymous.
- If active exploitation is detected, maintainers may publish a minimal
  mitigation note before a full fix and will say so in the advisory.

## What happens when a target is missed

Missing a target does not close the report. Maintainers must:

1. post a status update in the private advisory explaining the delay;
2. give a new estimated assessment or fix date; and
3. keep the reporter informed until the report is fixed or formally declined.

If the acknowledgement or assessment target has passed without a reply, the
reporter may send a follow-up private message to the maintainer. Do not move the
report to a public issue to get attention; that would expose users before a fix.

A report may be declined when it is out of scope (for example, a vulnerability
in a deployment's own configuration), already fixed on `main`, or dependent on
an unsupported fork. A decline is explained in the private advisory with the
reason.

## Scope

This process covers the code and shipped configuration in this repository. It
does not cover a fork's custom changes, third-party services, or the security of
an operator's deployment. See [SECURITY.md](../SECURITY.md) for supported
versions and repository security expectations.
