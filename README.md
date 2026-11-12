# mysql-component-ci

Reusable GitHub Actions releases for MySQL components, adapted from
[mariadb-plugin-ci](https://github.com/lefred/mariadb-plugin-ci).
Prepare a source/CMake SDK image once for each exact MySQL release, then build
only the requested component target and its CMake dependencies in release jobs.
The helpers never request a server or global build.

## Prepare an SDK

Set repository Actions secrets `SDK_REGISTRY_USER` and `SDK_REGISTRY_PASSWORD`
for a registry account with push access. Dispatch
[prepare-sdk.yml](.github/workflows/prepare-sdk.yml) with an exact `mysql_version`
(such as `8.4.6`) and your `image_repository`.
The default destination is `quay.io/lefred14/mysql-component-build`.
Images must be public for the reusable workflow; no images are assumed to exist.

The image uses the AlmaLinux 8 MariaDB build worker, its GCC/CMake toolchain,
and development dependencies.
It checks out the exact upstream `mysql-X.Y.Z` tag into
`/home/buildbot/mysql-server`, verifies the tag, detaches HEAD, and configures
`build/` with RelWithDebInfo, system OpenSSL, unit tests disabled, and Router disabled.
MySQL 8.0 can download its required Boost version during preparation. Newer
releases may require an updated toolchain or other dependencies; validate each
release before publishing its SDK. Additional CMake dependencies may download
during SDK preparation.

```bash
podman build -f sdk/Containerfile \
  --build-arg MYSQL_VERSION=8.4.6 \
  -t localhost/mysql-component-build:8.4.6 .
```

`BASE_IMAGE` must provide the AlmaLinux-compatible DNF packages, buildbot user,
`/home/buildbot/bin`, and build toolchain expected by the Containerfile.
`EXTRA_PACKAGES` supplies whitespace-separated RPM packages. This repository
uses the MariaDB worker image but does not add Galera configuration.
Built binaries require compatible glibc, libstdc++, and runtime libraries
on the destination; they are not universal Linux binaries.

For reproducibility, pin the base image digest, package repositories/versions,
and the final SDK digest. Release tags and mutable image tags alone are not
sufficient. The publication workflow reports the resulting image digest.

## Use from a component repository

Copy [examples/uuid_v7.yml](examples/uuid_v7.yml) to the component repository's
`.github/workflows/release.yml`:

```yaml
name: Release UUID v7
on:
  push:
    tags: ['v*']
  workflow_dispatch:
permissions:
  contents: write
jobs:
  build:
    uses: lefred/mysql-component-ci/.github/workflows/build-component.yml@main
    with:
      component_name: uuid_v7
      component_directory: uuid_v7
      build_target: component_uuid_v7
      component_library: component_uuid_v7.so
      mysql_versions: '8.4.6'
      run_tests: false
```

Pin the reusable workflow to a commit SHA for production. This workflow checks
out the caller's triggering commit with recursive submodules, links it under
`components/`, reruns CMake, and builds the component. A missing SDK image fails
rather than falling back to a different server release.

`mysql_versions` accepts whitespace-separated exact releases or a JSON array:

```yaml
      mysql_versions: |
        8.0.42
        8.4.6
      sdk_images: >-
        {"8.4.6":"quay.io/your-org/mysql-component-build@sha256:REPLACE_WITH_DIGEST"}
```

Prepare every requested image first and verify that the component supports every
selected release. These examples are version selections, not a maintained support
matrix. `sdk_image` changes the default registry repository; `sdk_images` overrides
individual full image references. Each image's recorded release must match.

Other inputs:

| Input | Purpose |
| --- | --- |
| `component_name` | Required package/documentation name |
| `component_library` | Required shared library filename |
| `component_directory` | Directory under `components/`; defaults to repository name |
| `build_target` | Optional explicit CMake target |
| `cmake_options` | Shell-quoted CMake arguments, parsed without shell evaluation |
| `extra_packages` | Optional RPM packages installed before configuration; prefer baking them into the SDK |
| `package_version` | Defaults to the Git tag or short commit SHA |
| `artifact_retention_days` | Defaults to 14 |
| `run_tests` | Defaults to false; requires a matching prebuilt MTR runtime when enabled |
| `test_runtime` | Absolute path inside a custom SDK to that runtime |
| `test_suite` | MTR suite name; defaults to component directory/repository name |
| `package_formats` | `tgz`, `rpm` and/or `deb`, whitespace-separated; defaults to all three |
| `package_license` | License in rpm/deb metadata (for example `GPL-2.0-only`); defaults to `Unknown` |
| `package_maintainer` | Debian `Maintainer`; defaults to the repository owner's GitHub noreply address |
| `rpm_requires` | Optional RPM `Requires`, for example `mysql-community-server = 8.4.6` |
| `deb_depends` | Optional Debian `Depends`, for example `mysql-server (>= 8.4.6)` |

When `build_target` is empty, the helper reads one literal `MYSQL_ADD_COMPONENT`
name and resolves the `component_<lowercase-name>` target. Multiple declarations
or computed names require an explicit target. CMake target help must confirm it.
Global targets and the built-in server component are rejected. A component can
still declare dependencies on large targets; check its CMake definitions.
Generated headers/tools and explicitly linked libraries may be built.

## Local build and package

With the prepared image available:

```bash
mkdir -p dist
podman run --rm --user root \
  -v "$PWD/dist:/out/dist:Z" \
  localhost/mysql-component-build:8.4.6 bash -c '
    set -euo pipefail
    build-component https://github.com/lefred/mysql-component-uuid_v7.git component_uuid_v7
    export GITHUB_WORKSPACE=/out
    export COMPONENT_SOURCE=/home/buildbot/mysql-server/components/mysql-component-uuid_v7
    export COMPONENT_NAME=uuid_v7 COMPONENT_LIBRARY=component_uuid_v7.so MYSQL_VERSION=8.4.6
    export PACKAGE_VERSION=$(git -C "$COMPONENT_SOURCE" rev-parse --short=12 HEAD)
    export COMPONENT_PATH=/home/buildbot/mysql-server/build/plugin_output_directory/component_uuid_v7.so
    package-component
  '
(cd dist && sha256sum --check *.sha256)
```

URL mode clones the default branch, or updates an existing clone using
`git pull --ff-only` followed by submodule updates. For a pinned local revision,
mount a checkout and set `COMPONENT_SOURCE` to its absolute path instead; it is
linked without updating it. CI always uses this mode. `MYSQL_SOURCE` overrides
the prepared source-tree path. `BUILD_JOBS` overrides build parallelism, which
otherwise defaults to the available CPUs. Use separate containers for concurrent
builds; do not replace a prepared tree with another source/build tree.

## Packages and installation

Tarballs contain:

```text
uuid_v7-v1.0.0-mysql8.4.6-linux-x86_64/
├── lib/plugin/component_uuid_v7.so
├── share/mysql/uuid_v7/README.md
├── share/mysql/uuid_v7/LICENSE
└── share/mysql/uuid_v7/INSTALL.txt
```

The package runs the component's own CMake install rules, so files it installs
with `INSTALL()` (configuration files, SQL scripts, ...) are packaged too, laid
out as in a MySQL server tarball (the SDK uses the `STANDALONE` layout: the
library goes to `lib/plugin/`, `INSTALL_MYSQLSHAREDIR` is `share/`). Only the
rules in `components/<component_directory>` run, never the server's. Absolute
destinations are kept inside the package. When the library is not installed by
those rules, it is copied to `lib/plugin/`. Set `COMPONENT_BUILD_DIR` (or
`COMPONENT_DIRECTORY`) to package install rules when running `package-component`
by hand.

Available README and license files are copied from the component repository.
Runtime dependencies are not bundled. `INSTALL.txt` directs users to query
`SELECT @@plugin_dir`, copy the library there (and any other packaged files
where the component's README says), and activate it using
`INSTALL COMPONENT 'file://component_uuid_v7'`. It also includes the matching
`UNINSTALL COMPONENT` command. See the component's README for configuration.

Builds upload tarballs, RPMs, debs and their SHA256 files as Actions artifacts. On `v*` tags,
a final job downloads the build artifacts, verifies checksums, and creates or
updates the caller repository's GitHub Release. Manual runs on branches only
upload artifacts. Packages target Linux x86_64 on the hosted runner and the exact
MySQL release used to build them.

### RPM and Debian packages

With `package_formats` containing `rpm` and/or `deb` (the default), the same
files are also packaged for the system layout used by MySQL's own packages:

| Tarball path | RPM | DEB |
| --- | --- | --- |
| `lib/plugin/` | `/usr/lib64/mysql/plugin/` | `/usr/lib/mysql/plugin/` |
| `share/mysql/<name>/` (docs) | `/usr/share/doc/mysql-component-<name>/` | same |
| `share/` | `/usr/share/mysql/` | same |
| `bin/` | `/usr/bin/` | same |
| `etc/`, `usr/`, ... (absolute destinations) | unchanged, `/etc` files are `%config(noreplace)` | unchanged, `/etc` files are conffiles |

```text
mysql-component-uuid-v7-1.0.0-1.mysql8.4.6.el8.x86_64.rpm
mysql-component-uuid-v7_1.0.0-1+mysql8.4.6_amd64.deb
```

The package name is `mysql-component-<name>` with `_` turned into `-` (a leading
`component_` is dropped). The version is the tag without its `v`; `-` becomes `~`
so `v1.2.0-rc1` sorts before `1.2.0`, and untagged builds get `0~<commit>`.
The MySQL release is part of the RPM release / Debian revision, since a component
only works with the server it was built against. `PACKAGE_RELEASE` (default 1)
sets the package release number when running `package-component` by hand.

Both formats are built inside the AlmaLinux 8 SDK (`rpmbuild`, and `dpkg-deb`
from EPEL). RPMs get automatic shared-library `Requires`; the `.deb` only gets
what `deb_depends` lists, and its binary is the same EL8-built library, so
check that its runtime libraries (for example OpenSSL 1.1 versus 3) exist on the
target Debian/Ubuntu release. No dependency on a server package is added unless
`rpm_requires`/`deb_depends` is set, because package names differ between
MySQL Community, distribution and Percona builds.

SDK images prepared before rpm/deb support install `rpm-build`/`dpkg` on the fly
but carry the old `package-component`; the workflow then stops with an error
unless `package_formats: tgz`. Rebuild those images to get the new helper.

## Optional MTR tests

Provide a derived SDK image containing a matching prebuilt MySQL distribution
with `mysql-test/mysql-test-run.pl`, the server, client/test executables, Perl
modules, and any auxiliary libraries/data the suite requires. Set `test_runtime`
to its absolute path and `run_tests: true`. Automatic runtime downloading and
server compilation are not implemented.

The caller supplies `mysql-test/suite/<test_suite>`. The workflow links this suite
into the runtime, verifies the server version, and passes a temporary plugin
directory containing the freshly built component to MTR. The suite should install
and uninstall the component itself. Suites needing additional plugins must arrange
for those libraries to be available in this directory or adjust the test setup.

## Validation

```bash
bash -n sdk/prepare scripts/package-component
python3 -m py_compile scripts/build-component
actionlint
```

These checks do not establish server ABI compatibility. Validate SDK preparation and an actual component on each selected
MySQL release before publishing images. Hosted Actions, registry publishing, and
MTR require separate integration validation.

Initial validation with the previous Ubuntu base: Bash syntax and actionlint
passed. The Ubuntu
container's dependency installation also completed. Full SDK preparation and a
real UUID v7 build remain unverified: the upstream source transfer failed over
HTTP/2, and the slow HTTP/1.1 retry was stopped before configuration. No SDK image
has been published.

The current AlmaLinux-based SDK still requires a complete container build to
validate its toolchain and dependencies against the selected MySQL release.
