# founderskills-test

**Disposable test mirror. Not the canonical repository.**

This repo exists only to install the `founder-skills` plugin into Claude Cowork for testing before a
release. It is a partial, throwaway copy:

- **History is replaced on every sync** — each deploy force-pushes a single fresh commit, so there is no
  meaningful git history here and links to old commits will break.
- **`version` in `plugin.json` is rewritten** to a reserved `0.X.9NN` release-candidate number so Cowork's
  version-keyed cache picks the build up. It does not correspond to any released version.
- **Tests are excluded**, and only the plugin directory is mirrored.

Use the real thing instead: **https://github.com/lool-ventures/founder-skills**

Licensed under Apache-2.0 (see `founder-skills/LICENSE`). The bundled Sora typeface is licensed
separately under the SIL Open Font License (see `founder-skills/references/brand/fonts/OFL.txt`).
