"""공정위 의결서 (FTC decisions) from the 법제처 DRF Open API (``target=ftc``).

- :mod:`.client`   — httpx client: search / decision XML / table image, rate limit, retries,
  on-disk cache under ``data/raw/ftc``.
- :mod:`.parse`    — decision XML -> sections, footnotes, table references (img flSeq, captions).
- :mod:`.extract`  — case-level facts (regex) with provenance (decision id + section offsets).
- :mod:`.collect`  — collection pipeline + manifests (``data/manifests/ftc*.json``).
- :mod:`.cli`      — ``jettae sources ftc fetch|images|facts``.

The development key ``OC=test`` is the DRF sample key; production use needs your own OC
(register at https://open.law.go.kr) passed via ``JETTAE_DRF_OC``.
"""
