# Week 18 llm-d release inputs

The runner accepts actual fixed-release artifacts instead of inventing stable plugin
field names. Bind these keys in `../platform-versions.lock.yaml`:

| Key | Required input |
|---|---|
| `llmd_load` | Rendered namespaced EPP Deployment/Service/RBAC/ConfigMap for load-aware |
| `llmd_prefix` | Same resource set/names for precise-prefix; isolate experimental changes |
| `load_pipeline` / `prefix_pipeline` | Actual plugin order, values, signal definitions and source permalinks |
| `cache_identity` | Model/tokenizer/template/adapter/namespace/Pod/process identity and reuse evidence interface |
| `gateway_glue` | Provider-specific namespaced ext_proc integration |
| `compatibility` | Fixed GAIE/Gateway/vLLM/llm-d compatibility and unsupported cells |

Use explicit namespace and `app.kubernetes.io/part-of: serving-study` labels. Preserve
`Deployment/vllm`, InferencePool and route from the shared generator. Artifacts should
reuse object names between pipelines so an apply cannot leave two competing EPPs.
If the release needs components outside this namespaced set, install and audit them
separately, and capture the dependency in the compatibility record.

`fault_<case>_apply` and `fault_<case>_restore` supply release-specific failures. They are
reviewed resource manifests, not executable shell hooks. No upstream code or image has
been downloaded, installed, or declared compatible by generating this repository code.

See [the Chinese walkthrough](../../docs/week-18-code-walkthrough.md).
