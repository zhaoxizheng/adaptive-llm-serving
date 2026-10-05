# Week 17 GAIE inputs

`base.template.yaml` is generated from `src.platform_contract.data_plane`: a two-replica
vLLM Deployment, identity and metrics Services, InferencePool v1, and HTTPRoute.
Image placeholders make this a review template, not a frozen deployment.

Export the **v1.0.0** CRD release bundle into this directory and bind `gaie_crds` in
`../platform-versions.lock.yaml`. Separately pin a compatible reference EPP source commit,
image digests, Deployment/Service/RBAC as `reference_epp`, and provider-specific namespaced
configuration as `gateway_glue`. The existing Gateway is a prerequisite.

Both artifact bundles must use the dedicated namespace and the label
`app.kubernetes.io/part-of: serving-study`. EPP Service/Deployment defaults are `epp`;
align their discovery selector, inference port 8081 and engine metrics port 8000.
Do not duplicate objects already supplied by the base generator.

For official conformance, bind one reviewed Job as `conformance_job`, with
`activeDeadlineSeconds <= 1800` and `backoffLimit: 0`. Pin its test image and scope;
install any required cluster RBAC separately. No conformance result is included here.

See [the Chinese walkthrough](../../docs/week-17-code-walkthrough.md).
