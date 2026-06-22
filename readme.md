# tfdriftmonitor

Monitors Terraform provider version drift across GitHub repos — flags when a pinned provider version (e.g. `aws ~> 5.0`) falls behind what HashiCorp's registry has available, with AI-assisted changelog analysis on major version bumps to judge whether breaking changes actually affect resources you use.

This project is a deliberate continuation of `fasthd97/driftmonitor`, which monitors AWS CloudFormation stacks for Lambda runtime EOL drift. That tool is already deployed and running.

This repository extends the same underlying idea — catching silent dependency drift before it causes an incident — into the Terraform/provider-version space, with a full CI/CD security pipeline built around it.

**Status:** actively being built. Sections below marked *(coming soon)* cover features not yet implemented.

---

## How it works