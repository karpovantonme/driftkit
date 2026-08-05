# Territory

Who is working where, so two people do not land on the same project on the same day.

**This is a courtesy list, not a claim of ownership.** Nobody owns a repository they do not maintain. The point is only to avoid what happened on 2026-08-05: two contributors ran this checker against the same project a day apart, both patches were correct, and the maintainer closed both, because two similar automated pull requests in a row read as spam.

## How to use it

Before opening a pull request, check whether the project is listed. If it is, pick another one or write to whoever is there. If it is not, add yourself in a pull request to this file: those are merged without discussion.

This file will always be behind, so also check the project's own list:

```console
gh pr list --repo OWNER/NAME --state all --limit 50 --search "docstring OR param OR doxygen"
```

---

## Anton Karpov ([@karpovantonme](https://github.com/karpovantonme))

Contact: open an issue here.

Everything below is already visible on the profile, this list just saves you the click.

### Projects with pull requests open or merged

- `AFLplusplus/AFLplusplus`
- `ARC-OPT/wbc`
- `DLR-AMR/t8code`
- `ESMValGroup/ESMValCore`
- `GyulyVGC/sniffnet`
- `JetBrains/kotlin-web-site`
- `adtzlr/felupe`
- `alan-turing-institute/autoemulate`
- `astropy/astroquery`
- `astropy/photutils`
- `astropy/pyvo`
- `boostorg/algorithm`
- `boostorg/geometry`
- `boostorg/gil`
- `boostorg/graph`
- `boostorg/histogram`
- `boostorg/json`
- `boostorg/math`
- `boostorg/thread`
- `cadet/CADET-Core`
- `deepinv/deepinv`
- `e2b-dev/E2B`
- `etcd-io/etcd`
- `flekschas/jupyter-scatter`
- `hajimes/mmh3`
- `idaholab/MontePy`
- `karmada-io/karmada`
- `koide3/small_gicp`
- `kubernetes/website`
- `mlco2/ecologits`
- `mne-tools/mne-python`
- `networkx/networkx`
- `nf-core/chipseq`
- `nf-core/modules`
- `nilearn/nilearn`
- `open-telemetry/opentelemetry-collector`
- `opencv/opencv`
- `pandas-dev/pandas`
- `prometheus/prometheus`
- `rclone/rclone`
- `statsmodels/statsmodels`
- `supabase-community/postgres-language-server`
- `supabase/splinter`
- `supabase/supabase`
- `supabase/supabase-py`
- `thanos-io/thanos`
- `traefik/traefik-helm-chart`
- `vprusso/toqito`
- `weaviate/weaviate-io`
- `xgi-org/xgi`

### Boost, all of it

Working through the Doxygen tags across the whole organisation, one library at a time: `accumulators`, `algorithm`, `asio`, `beast`, `bimap`, `circular_buffer`, `compute`, `container`, `date_time`, `dynamic_bitset`, `fiber`, `filesystem`, `function`, `geometry`, `gil`, `graph`, `heap`, `histogram`, `icl`, `interprocess`, `iterator`, `json`, `lockfree`, `math`, `mpl`, `multiprecision`, `optional`, `polygon`, `preprocessor`, `process`, `program_options`, `python`, `random`, `regex`, `safe_numerics`, `serialization`, `signals2`, `smart_ptr`, `spirit`, `test`, `thread`, `tokenizer`, `units`, `url`, `uuid`, `variant`

---

## Add yourself below

Copy the shape above: your handle, how to reach you, the projects you are on. Project names are enough, no need to say what you are fixing.
