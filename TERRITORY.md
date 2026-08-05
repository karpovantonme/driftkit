# Territory

Who is working where, so two people do not land on the same project on the same day.

**This is a courtesy list, not a claim of ownership.** Nobody owns a repository they do not maintain. The point is only to avoid what happened on 2026-08-05: two contributors ran this checker against the same project a day apart, both patches were correct, and the maintainer closed both because two similar automated pull requests in a row read as spam.

## How to use it

Before opening a pull request, check whether the project is listed. If it is, pick another one or write to whoever is there. If it is not, add it in the same pull request as your work, or send a one-line change to this file. Those are merged without discussion.

Also check the project's own pull request list, this file will always be behind:

```console
gh pr list --repo OWNER/NAME --state all --limit 50 --search "docstring OR param OR doxygen"
```

When you are done with a project, leave it listed. A merged fix is history worth keeping.

---

## Anton Karpov ([@karpovantonme](https://github.com/karpovantonme))

Contact: open an issue here.

### Pull requests opened

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

Working through the Doxygen tags across the whole organisation, one library at a time:

`accumulators`, `algorithm`, `asio`, `beast`, `bimap`, `circular_buffer`, `compute`, `container`, `date_time`, `dynamic_bitset`, `fiber`, `filesystem`, `function`, `geometry`, `gil`, `graph`, `heap`, `histogram`, `icl`, `interprocess`, `iterator`, `json`, `lockfree`, `math`, `mpl`, `multiprecision`, `optional`, `polygon`, `preprocessor`, `process`, `program_options`, `python`, `random`, `regex`, `safe_numerics`, `serialization`, `signals2`, `smart_ptr`, `spirit`, `test`, `thread`, `tokenizer`, `units`, `url`, `uuid`, `variant`

### Surveyed and in the queue

- `CERN/TIGRE`
- `ComputationalBiomechanicsLab/opensim-creator`
- `ERGO-Code/HiGHS`
- `FelixKrueger/TrimGalore`
- `NOAA-EMC/UPP`
- `NOAA-EMC/ufsatm`
- `OpenMD/OpenMD`
- `PyTables/PyTables`
- `RBVI/ChimeraX`
- `SynxFlow/SynxFlow`
- `TUMFTM/PointCloudCrafter`
- `TorchIO-project/torchio`
- `ankitpokhrel/jira-cli`
- `argoproj/argo-cd`
- `arviz-devs/arviz`
- `astropy/astropy-healpix`
- `axboe/fio`
- `cvanaret/Uno`
- `cvs-health/langfair`
- `cvxpy/cvxpy`
- `devosoft/Empirical`
- `dfki-ric/ugv_nav4d`
- `ekiefl/pooltool`
- `emlearn/emlearn-micropython`
- `forefireAPI/forefire`
- `gnss-sdr/gnss-sdr`
- `grafana/loki`
- `hypre-space/hypre`
- `ibis-project/ibis`
- `karpovantonme/splinter`
- `karpovantonme/supabase-py`
- `lenstronomy/lenstronomy`
- `materialsproject/pymatgen`
- `medialab/xan`
- `mesa/mesa`
- `munich-quantum-toolkit/core`
- `nasa/HyperCP`
- `nasa/fpp`
- `nf-core/atacseq`
- `nf-core/callingcards`
- `nf-core/epitopeprediction`
- `nf-core/magmap`
- `nf-core/metatdenovo`
- `nf-core/methylseq`
- `nf-core/nanoseq`
- `nf-core/nascent`
- `nf-core/panoramaseq`
- `nf-core/proteinfold`
- `nf-core/raredisease`
- `nf-core/riboseq`
- `nf-core/rnaseq`
- `nf-core/rnasplice`
- `nf-core/sarek`
- `nf-core/scrnaseq`
- `nf-core/smrnaseq`
- `nf-core/viralrecon`
- `nipy/heudiconv`
- `open-telemetry/opentelemetry.io`
- `pathsim/pathsim`
- `posit-dev/great-tables`
- `prashjha/PeriDEM`
- `probml/dynamax`
- `pyTMD/pyTMD`
- `pyro-ppl/pyro`
- `python-visualization/folium`
- `qdrant/qdrant`
- `qutip/qutip`
- `sandialabs/pyGSTi`
- `scikit-hep/vector`
- `scikit-image/scikit-image`
- `scverse/anndata`
- `skforecast/skforecast`
- `snakemake/snakemake`
- `solids4foam/solids4foam`
- `sourmash-bio/sourmash`
- `spacetelescope/jdaviz`
- `tqec/tqec`
- `votca/votca`
- `vuejs-translations/docs-ru`
- `wrenfold/wrenfold`

---

## Add yourself below

Copy the shape above: a heading with your handle, how to reach you, and the projects you are working on. Keep it to project names, no need to explain what you are fixing.
