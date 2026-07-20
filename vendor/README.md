# Vendored tidal-dl-ng

`tidal_dl_ng-0.32.0-py3-none-any.whl` is repackaged from the copy of
`tidal-dl-ng` 0.32.0 already installed on this machine.

**Why it's vendored instead of pulled from PyPI/GitHub at build time:**
As of 2026-07-19, `tidal-dl-ng` is no longer published on PyPI
(`https://pypi.org/simple/tidal-dl-ng/` returns 404) and the upstream
source repo `github.com/exislow/tidal-dl-ng` is also gone (404 on the
GitHub API). Without a live upstream source, the Docker build can't `pip
install tidal-dl-ng` normally, so this wheel — rebuilt from the still-valid
local install (same files, same hashes, unmodified) — is checked in so the
image stays reproducible.

It's a pure-Python wheel (`py3-none-any`), so the `.py` files inside it
*are* the source — nothing is compiled or obfuscated. License (AGPL-3.0)
is bundled in the wheel's `dist-info/licenses/LICENSE` and duplicated here
as `TIDAL-DL-NG-LICENSE`.

If upstream reappears (or a fork becomes the canonical source), prefer
switching `requirements.txt` back to a normal PyPI/git dependency and
deleting this directory.
