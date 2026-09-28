---
name: azure-architecture
description: Use when designing or changing an Azure solution architecture (choosing services, SKUs, regions, identity, networking) before any IaC is written. Not for reviewing existing Terraform (use terraform-review) or for general research (use research).
---

# Azure architecture design

## Steps

1. **Pin the requirements.** Write down in 5–10 bullets: workload, users,
   data sensitivity, availability target, monthly cost ceiling, region
   constraints. Ask the user for anything missing that changes the design.
2. **Check current facts, not memory.** For each candidate service confirm on
   learn.microsoft.com and the Azure retail prices API
   (`https://prices.azure.com/api/retail/prices`): region availability, SKU
   limits, quotas, pricing unit, minimum charges. Check subscription reality
   with `az` (`az account show`, `az provider show -n <ns>`,
   `az vm list-usage -l <region>` or `az quota` as relevant).
3. **Choose identity first.** Default to managed identities and Entra ID RBAC
   with the narrowest built-in role at the narrowest scope. Avoid account keys,
   SAS and client secrets; if one is unavoidable, name where it lives and how
   it rotates.
4. **Choose network exposure explicitly.** For every endpoint state: public or
   private, what authenticates callers, and TLS. No anonymous write endpoints.
5. **Estimate cost.** Produce a table: resource, SKU, pricing unit, assumed
   usage, monthly estimate, free grants, minimum/idle charges. Flag anything
   billed while idle.
6. **Write the result** as a short design note: diagram (text or Mermaid),
   components, identity/RBAC matrix, exposure table, cost table, open risks.
   Mark each claim as verified (with link) or assumption.
7. **Record the decision** with `decision_add` (or in `shared/DECISIONS.md`
   via review), listing the alternatives rejected.

## Output checklist

- [ ] Every service has region + SKU + verified price source
- [ ] Identity and RBAC matrix with scopes
- [ ] Idle cost stated
- [ ] Teardown path stated
