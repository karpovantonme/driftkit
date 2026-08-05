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

## Organisations, not just repositories

**This is the part people get wrong, and it cost us a closed pull request.**

A repository is not the unit that matters. An organisation is. `astropy` alone has dozens of repositories and a maintainer set that overlaps heavily between them: the person reviewing your patch in one repo reads the others too. Two similar automated pull requests landing anywhere in that organisation within a day read as a campaign, even when they sit in different repositories and both patches are correct.

That is exactly what happened to us. One pull request in `astropy/astroquery` from one contributor, another the next morning from a second contributor, both correct, both closed, with the maintainer saying he wanted to discourage the pattern.

So the rule is: **if any name below appears as the owner of the repository you are about to touch, treat it as taken, whatever the repository is called.** Ask first.

| Organisation | Our pull requests |
|---|---:|
| `boostorg/*` | 15 |
| `supabase/*` | 5 |
| `astropy/*` | 4 |
| `mne-tools/*` | 3 |
| `nilearn/*` | 3 |
| `GyulyVGC/*` | 2 |
| `cadet/*` | 2 |
| `flekschas/*` | 2 |
| `kubernetes/*` | 2 |
| `networkx/*` | 2 |
| `nf-core/*` | 2 |
| `prometheus/*` | 2 |

Plus one pull request each in: `AFLplusplus`, `ARC-OPT`, `DLR-AMR`, `ESMValGroup`, `JetBrains`, `adtzlr`, `alan-turing-institute`, `deepinv`, `e2b-dev`, `etcd-io`, `hajimes`, `ibis-project`, `idaholab`, `karmada-io`, `koide3`, `mlco2`, `open-telemetry`, `opencv`, `pandas-dev`, `posit-dev`, `rclone`, `scverse`, `statsmodels`, `supabase-community`, `thanos-io`, `traefik`, `vprusso`, `weaviate`, `xgi-org`.

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

## Dmitry Rantovov ([@darkdi](https://github.com/darkdi))

PHP and Bitrix by trade, working outward from there. Reachable through issues on
this repository.

| Organisation | Pull requests |
|---|---:|
| `bagisto/*` | 3 |
| `statamic/*` | 2 |
| `raycast/*` | 2 |
| `astropy/*` | 2 |
| `InvoicePlane/*` | 1 |
| `dask/*` | 1 |
| `getgrav/*` | 1 |
| `home-assistant/*` | 1 |
| `laravel/*` | 1 |
| `nlohmann/*` | 1 |
| `php-curl-class/*` | 1 |
| `tesseract-ocr/*` | 1 |

### Projects with pull requests open or merged

- `InvoicePlane/InvoicePlane`
- `astropy/astropy`
- `bagisto/bagisto`
- `dask/dask`
- `getgrav/grav`
- `home-assistant/core`
- `laravel/telescope`
- `nlohmann/json`
- `php-curl-class/php-curl-class`
- `raycast/extensions`
- `statamic/cms`
- `tesseract-ocr/tesseract`

### Checked and clean, no findings

Not worth a second pass: `EasyAdminBundle`, `FreshRSS`, `biopython`, `firefly-iii`,
`fmt`, `godot`, `imgui`, `kimai`, `libuv`, `libzmq`, `mautic`, `obs-studio`,
`poweradmin`, `spdlog`, `zarr-python`.

### False positives worth knowing about

Places where the checker fires but the documentation is correct:

- `flarum/framework` — 20 of 20. A modern PHPStan type carries its own named
  arguments inside parentheses: `@param (callable(Configurator $configurator): void)|class-string $callback`.
  Taking the first `$var` in the line grabs `$configurator` rather than `$callback`.
- `libgit2/libgit2` — 22 of 22. `typedef int (*git_commit_create_cb)(...)` and the
  `GIT_CALLBACK` macro are not parsed as declarations, so every documented
  argument of the callback is reported as missing. Tell-tale sign: all findings
  sit on one or two line numbers.
- `Dolibarr/dolibarr` — all in `htdocs/includes/sabre/`, a vendored library.
- `pixelfed/pixelfed` — `app/Util/Lexer/` looks first-party but the header says
  `Copyright © 2010, Nick Pope`: vendored twitter-text. Check the file header,
  not just the path.
- `duckdb/duckdb` — the findings sit in a file that is corrupt (one class
  declared twice, one line spliced from two) and included nowhere.
