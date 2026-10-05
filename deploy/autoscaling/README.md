# Week 20 mutually exclusive scaling paths

`hpa/resource.yaml` and `keda/resource.yaml` target the same ordinary `Deployment/vllm`.
They both use workload-total queue pressure with `AverageValue`, bounded to 1–2 replicas.
HPA uses an Object metric for the Deployment via custom.metrics; KEDA owns external.metrics.
The adapter values intentionally disable external rules to avoid an APIService collision.
Never apply both. Use the study runner's explicit handoff and `--restore` to fixed two.
`fixed-1.patch.yaml` / `fixed-2.patch.yaml` are strategic merge patches, not standalone
Deployments. The runner uses `/scale` for this bounded manual operation.

`metric-rules.example.yaml` is a Prometheus rule-file input. Configure and freeze scraping
so namespace/workload/pod/model_name identify exactly the intended engine series. The
rule rejects stale queue/Ready samples and incomplete per-Pod coverage; missing data is
not converted to zero. Validate this candidate against the actual fixed metric schema.

`prometheus-adapter-values.example.yaml` is a **chart values input**, not a Kubernetes
manifest or an installed adapter. Pin the compatible chart/image, render and install the
adapter separately, then bind the actual mapping as `adapter_values`, the rule file as
`metric_rules`, and scrape configuration as `metrics_scrape`. Preserve custom API
discovery/APIService health and compare responses to HPA currentMetrics.

KEDA's scaler accesses `metric.prometheus_url` from the cluster; the local evidence
collector uses `metric.local_query_url` (for example an explicit private port-forward).
These endpoints must read the same Prometheus data. Pin KEDA operator and metrics-server
images. Set `replicas_sync_disabled` only after disabling continuous replicas sync.

No scale-to-zero overlay is provided for the default SLO matrix. Startup/activation and
first-request guarantees must be established in a separate experiment.

The Week 15 `hpa.yaml` remains a CPU baseline; it must not coexist on the Week 20 target.
The dashboard's client panels can scrape `scripts/serve_platform_metrics.py`, which reads
one live session and binds only to localhost. This provides current samples, not historical
backfill. Keep the Prometheus/adapter/KEDA failure targets inside the independent lab;
fault artifacts cannot modify a shared monitoring namespace.
See [the Chinese walkthrough](../../docs/week-20-code-walkthrough.md).
