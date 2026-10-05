# Fixed-release LLMInferenceService declarations

Prepare `managed.yaml` and `referenced.yaml` from the **same pinned KServe release**.
Each must contain its minimal LLMInferenceServiceConfig/baseRefs and LLMInferenceService
declaration, plus only the namespaced dependencies the chosen release actually needs.
Bind them as `kserve_managed` / `kserve_referenced` in `../versions.lock.yaml`.

No alpha apiVersion is preselected. Fill `kserve_api_version` from the installed CRD and
bind its release schema as `kserve_crds`; do not change an example's version string to
make it appear compatible. Use `vllm-audit` as the parent name or update the study config.

Keep fixed replicas and complete single-node GPU/TP replicas. Configure the selected
route's endpoint in `configs/week19-resource-audit.yaml`, and remove conflicting old lab
routes before generation smoke. Never edit the generated Deployment to change the model.

Prepare a one-object `mutation.yaml` as `kserve_mutation`: change a harmless **spec** field
supported by the release. Namespace and `app.kubernetes.io/part-of: serving-study` labels
are mandatory on every applied object. Shared CRD/controller/Gateway installations are
reviewed separately. Test both managed and referenced inputs with server-side dry-run.

These files are required runtime inputs; they are not silently generated using an
unverified alpha schema. See [the Chinese walkthrough](../../../docs/week-19-code-walkthrough.md).
