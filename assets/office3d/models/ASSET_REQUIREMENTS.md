# Office human assets

No usable human model is included in the repository. `manifest.js` therefore
contains five empty model URLs. The loader shows no substitute character and
makes no model request until a complete asset is configured.

## Required delivery

Deliver five textured, skinned GLB files with realistic faces, adult proportions,
and the clothing and hairstyles in the supplied reference sheets:

| Role key | Physical name tag | Appearance |
| --- | --- | --- |
| `market` | HERMES AGENT | Man, black suit, white/ivory shirt, teal tie, swept black hair, clean-shaven |
| `neuro` | NEUROBRO | Woman, black blazer and skirt, ivory blouse, black heels, long side-parted dark hair |
| `risk` | RISK MANAGER | Woman, same office outfit, dark bob haircut |
| `trading` | BINANCE | Woman, same office outfit, dark high ponytail |
| `boss` | HARUN | Man, black suit, white/ivory shirt, black tie, swept black hair, light stubble |

Each GLB must contain a humanoid skeleton with skin weights and embedded base
color textures. Include in-place clips for typing at a desk, standing idle,
seated idle, walking, conversation, sitting down, standing up, drinking coffee,
resting at the pool edge, and visiting/conversing with Harun. A compatible
conversation clip can be shared by `SOCIAL` and `VISITING`. Foot contacts,
chair/keyboard alignment, and transitions need visual review in the application.
Use metres, Y up, feet at zero, local +Z forward. Keep textures and buffers local
in this directory, preferably embedded in GLB. Target 10–35k triangles and
1k–2k textures per character for mobile; preserve a normal/roughness material
setup when supplied. These budgets are targets, not a replacement for quality.

Include a license/provenance file for every model and its animation clips. It
must permit the intended web use **and distribution of the source model and
textures in this public GitHub repository**. Record the source URL, license name,
local license file, `allowsWebUse`, and `allowsPublicRedistribution` in the
manifest. Set `referenceReviewed: true` only after comparing the model against
the reference image. Map the exact animation clip names in `clips` and set
`url`, relative to `avatars.js`, such as `./models/hermes-agent.glb`.
Set the exact nonempty `headBone` name. It must identify a Bone belonging to
the loaded skin skeleton; labels and pedestrian head avoidance follow this
animated bone through seated, typing, and walking poses.

## Audit evidence

- Existing assets consisted of procedural mascot geometry, without GLB/glTF/FBX
  models or human-model license files.
- [MakeHuman's official license](https://www.makehumancommunity.org/about/license.html)
  describes CC0 core assets. This permits an asset-creation route; it does not
  supply five finished matching characters and their office animation clips.
- [Renderpeople's free models](https://renderpeople.com/free-3d-people/) include
  scanned rigged people. The free page alone does not establish permission to
  redistribute the raw assets in this public repository or supply all five
  matching characters and required motions.
- The official [Innerscene Harper business-suit model](https://www.innerscene.com/tools/library/3d-parts/harper-3d-person-woman-business-suit-standing-9fde7f08)
  was inspected outside the repository. Its GLB embeds a CC0 copyright statement
  and textures, but contains **zero skins and zero animation clips**. Its
  short-hair/trouser outfit also does not match the three female references.
  It was not added as a substitute.

`avatarAssetReadiness().ready` remains false until all five manifest entries are
complete. Runtime loading additionally validates the actual skins, textures,
and named clips; metadata alone does not establish that a model can animate.
