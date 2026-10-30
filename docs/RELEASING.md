# Releasing Mira

The `release-tag-build` workflow creates a draft GitHub Release. It publishes the Python and unsigned iOS artifacts to that draft. Its Android APK uses an ephemeral CI debug key and remains an Actions artifact for package verification only.

Before creating a tag, run a preflight build with `workflow_dispatch`: set `release_tag` to the intended version, `source_ref` to its full commit SHA, and `publish_release` to `false`. A nonempty `source_ref` always suppresses release publication. Once that commit passes, tag the same commit; the tag build creates the draft. Keep failed tags unchanged.

The iOS workflow uses the official Zig 0.16.0 archive with a fixed SHA-256 digest for Frida 16.0.7's musl build. It validates archive entries before extracting into `RUNNER_TEMP` and passes the absolute executable path through `ZIG`. For a matching local build, set `ZIG=/absolute/path/to/zig`; when omitted, the build script keeps its Homebrew/PATH discovery. This keeps the older Vala-generated C sources on the compatible Clang 21 toolchain family.

Before creating `Mira.app.zip` and `Mira-unsigned.ipa`, `mira-build` copies the Xcode product to a temporary staging directory, excludes sensitive `.env` variants other than `.env.example`, removes embedded signatures from every Mach-O file, and deletes `_CodeSignature` directories and embedded provisioning profiles. The Xcode product itself is not modified. Packaging fails if signature inspection is inconclusive or a detected signature remains after removal, so both public iOS archives are genuinely unsigned and ready for downstream signing.

Build the public Android APK with the maintainer's existing local debug key:

```bash
MIRA_FRIDA_COMPAT_OUTPUT_DIR="$PWD/dist/android" \
MIRA_FRIDA_COMPAT_OUTPUT_APK="$PWD/dist/android/mira-app-debug.apk" \
  ./tools/android/build-frida-16.7-compat-apk.sh
```

Before publishing the draft:

1. Verify the APK version, Frida version, SHA-256 digest, ZIP alignment, signature, and expected signing certificate.
2. Upload it as `mira-app-debug.apk` and record its digest in the release notes.
3. Download every public asset and compare it with the verified source artifact.
4. Confirm the draft contains `mira-app-debug.apk`. Never publish a draft without the Android APK.

Do not upload a signing key to GitHub Actions. A certificate change prevents Android from installing the release as an update over builds signed by the previous certificate.
