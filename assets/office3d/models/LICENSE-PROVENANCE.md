# Avatar source provenance

This file records the source assets used to author the five Office character files. It does not assert visual approval, completion of application integration, or successful Blender MCP connection.

## Third-party source assets

All selected third-party meshes, rig data, targets and texture sources below are published as CC0 1.0 Universal. Their source notices and the official asset catalogs were checked. The MPFB program itself is GPL-3.0-or-later; its code license is distinct from the CC0 asset license and does not convert the exported character assets into GPL program code.

| Component | Source and credit | License evidence |
| --- | --- | --- |
| Human base, phenotype targets and game-engine skeleton | MakeHuman / MPFB team; MPFB source commit `6ff1eb358f6c1959ced77e79e6dd2406d9a71f49` | [MPFB asset/output license](https://github.com/makehumancommunity/mpfb2/blob/6ff1eb358f6c1959ced77e79e6dd2406d9a71f49/LICENSE.md), sections C and D; `LICENSE.ASSETS.md` |
| Skin, eyes, eyebrows, eyelashes, teeth, tongue, short/long/bob/ponytail hair, male shoes04 | MakeHuman system asset pack and its credited contributors | [Official system asset catalog](https://static.makehumancommunity.org/assets/assetpacks/makehuman_system_assets.html); source notices |
| Male jacket, shirt, tie and trousers; female blazer, blouse and skirt | Margaret Toigo / MRT | [Official suits01 catalog](https://static.makehumancommunity.org/assets/assetpacks/suits01.html); both selected `.mhclo` files state CC0 |
| Female T-bar heels | Cortu | [Official shoes01 catalog](https://static.makehumancommunity.org/assets/assetpacks/shoes01.html), `cortu_t-bar` entry |

CC0 source license: <https://creativecommons.org/publicdomain/zero/1.0/>. The third-party source terms permit web use and public redistribution of the source assets. Exact files, hashes, source pack and per-role use are recorded in the adjacent machine-readable provenance report.

## Project-authored additions

Character proportions, target selection, black/ivory outfit material assignments, tie colors, skin/hair tint factors and the fitted garment adjustments are authored in this project's `builder.py`. Source textures are embedded in the exported GLBs; some small details are downsampled by the builder for size. The source-image hashes remain recorded for those derivatives.

The ten named animation clips, their trajectory/IK/finger keyframes, and the coffee mug, handle and liquid meshes are procedural work authored in this project's animation script. No external motion-capture files, purchased animation packs or external cup model are used. These additions were created for Harun's project; this handoff imposes no additional restrictions on their use or redistribution. This statement does not transfer or relicense unrelated Office source code.

## Intended role mapping

| Office role | Output file | Source hairstyle | Source outfit |
| --- | --- | --- | --- |
| `market` / HERMES AGENT | `hermes-agent.glb` | `short01` | Toigo male suit; teal tie; shoes04 |
| `neuro` / NEUROBRO | `neurobro.glb` | `long01` | Toigo female suit; Cortu T-bar heels |
| `risk` / RISK MANAGER | `risk-manager.glb` | `bob01` | Toigo female suit; Cortu T-bar heels |
| `trading` / BINANCE | `binance.glb` | `ponytail01` | Toigo female suit; Cortu T-bar heels |
| `boss` / HARUN | `harun.glb` | `short01` | Toigo male suit; black tie; shoes04 |

This table is an output mapping, not an assertion that the reference sheet's exact face, hairstyle, beard or fabric detail has been reproduced. Any unverified appearance and motion differences must remain visible in the final review report.

## Software tooling

The upstream `mcp-for-blender` bridge was inspected at commit `2e4b0bc294694b1570d76ed01be657c22e6d02a0` (MIT). A bridge connection was not established in this executor. The actual asset authoring uses Blender 4.5.15's Python API and MakeHuman/MPFB assets. This provenance file does not claim MCP authored or controlled the delivered files.

## Office integration refinements — 2026-11-07

The supplied Blender sources were refined locally with Blender 4.3.2. Authored
additions include eyelid/lash blink shape keys, Harun's light head-skinned
stubble, clearer Risk Manager fringe and ivory blouse collar assignments.
Female resting wrist targets were moved clear of the jacket and all ten clips
rebaked with no unreachable IK targets. The original embedded texture image
bytes are unchanged in every final GLB. These project-authored additions impose
no additional redistribution restrictions. Original source license notices
above continue to apply.

`../review/avatar-reference-review.json` records final GLB and refined Blender
SHA256 values, the preserved source-file locations and visual review limits.
The restored 42-file package remains byte-for-byte intact in the work folder.
