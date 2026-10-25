# Porting a three.js scene to the vanilla renderer

This guide is a repeatable procedure for moving a scene authored in
[`threejs.html`](threejs.html) (three.js r170, rasterized) to
[`renderer.html`](renderer.html) (dependency-free WebGL2 BVH path tracer).

The two pages are built as mirrors of each other. They share the same page chrome,
the same studio constants, the same camera framing math, and the same final tone
pipeline (vignette → ACES → sRGB → grain). As a result, `threejs.html` is a
**faithful preview** of what `renderer.html` will produce, and porting is mostly
mechanical:

1. author in `threejs.html`;
2. export one `.glb`;
3. copy over any constants you changed;
4. optionally apply a small patch if your scene uses per-material roughness or metalness;
5. verify with an image diff.

The two images will never be pixel-identical, because one is a rasterizer and the
other a path tracer (see [§6](#6-expected-residual-differences)). A correct port of
the default elephant scene diffs at **≈6.4/255 mean absolute error**. The worked
example in [§7](#7-worked-example) diffs at **≈7.2/255**.

---

## Contents

1. [Prerequisites](#1-prerequisites)
2. [How the two files line up](#2-how-the-two-files-line-up)
3. [Shared conventions (do not break these)](#3-shared-conventions-do-not-break-these)
4. [Feature support matrix](#4-feature-support-matrix)
5. [The porting procedure](#5-the-porting-procedure)
6. [Expected residual differences](#6-expected-residual-differences)
7. [Worked example](#7-worked-example)
8. [Troubleshooting](#8-troubleshooting)
9. [Appendix: renderer.html scene data layout](#9-appendix-rendererhtml-scene-data-layout)

---

## 1. Prerequisites

- **Serve the repository root over HTTP.** Both pages `fetch()` the model from
  `../common/files/`, and browsers block that on `file://`. Use `run.command`
  (macOS) or `run.bat` (Windows), which run `http-server` from the repo root, or:

  ```sh
  cd gemini-4-demo-vanilla-3D
  python3 -m http.server 8080
  # http://127.0.0.1:8080/javascript/threejs.html
  # http://127.0.0.1:8080/javascript/renderer.html
  ```

- **A WebGL2 browser with `EXT_color_buffer_float`.** Any current Chrome, Edge,
  Firefox or Safari will do.
- **Network access for `threejs.html`.** It imports three.js r170 from
  `cdn.jsdelivr.net` through an import map. `renderer.html` has no dependencies.
- **Optional, for automated verification ([step 9](#step-9--verify-with-an-image-diff)):**
  Python 3 with `numpy`, `pillow` and `playwright`
  (`pip install playwright && playwright install chromium`).

Both pages accept the same URL parameters:

| Parameter | Meaning | Default |
| :--- | :--- | :--- |
| `model` | URL of a `.glb`, relative to `javascript/` | `../common/files/elephant.glb` |
| `yaw` | Orbit yaw in degrees | `-142` |
| `pitch` | Orbit pitch in degrees (downward positive) | `11` |

Both pages also accept a `.glb` dropped onto the frame, and both let you drag the
image to orbit.

---

## 2. How the two files line up

Each section of `threejs.html` names the `renderer.html` construct it stands in
for. Find things by **function or constant name**, not line number.

| Concern | `threejs.html` (three.js) | `renderer.html` (vanilla) |
| :--- | :--- | :--- |
| Studio constants | §1 `SUN_RADIANCE`, `SKY_*`, `GROUND_*`, `MESH_ROUGHNESS`, … (JS arrays) | JS constants at the top of the IIFE **plus** GLSL `const`s at the top of `FS_TRACE` |
| Your scene content | §2 `buildSceneContent(content)` | *(nothing: it arrives as the exported `.glb`)* |
| Model loading | `GLTFLoader.parse()` in `parseGlb()` | Hand-written `parseGlb()` → flat triangle soup |
| Material policy | `applyStudioMaterials()` | Per-triangle color in `buildBvhTextures()`; roughness/F0 in `traceScene()` (GLSL) |
| Normalization | `normalizeContentToStudio()` on the `studioRoot` group | `normalizeMeshToStudio()` on the vertex array |
| Camera framing | `buildCameraBasis()`, `frameCameraToMesh()` | Same two functions, line-for-line |
| Sun direction | `sunDirection()` → `DirectionalLight` position | `sunDirection()` → `u_sunDir` uniform |
| Sun shading | `DirectionalLight` (color × intensity = `SUN_RADIANCE`) | `cookTorranceBrdf() * SUN_RADIANCE * sunVis` |
| Sun shadow | VSM shadow map, `sun.shadow.radius` | One ray per sample, jittered over a disc of `SUN_TAN_RADIUS` |
| Sky diffuse | `HemisphereLight` with colors × π | `skyAmbient()` |
| Sky specular | PMREM of the sky dome, specular only (`injectStudioIndirect`) | `evaluateStudioBackdrop(envDir) * (f0*dfg.x + dfg.y)` |
| Ambient occlusion | `GTAOPass` (output Off), sampled in `injectStudioIndirect` for indirect light only | One cosine-weighted ray per sample, `AO_MAX_DIST` |
| Ground | `CircleGeometry` disc + fade injected in `onBeforeCompile` | Analytic `y = 0` plane in `traceScene()`; fade in `renderSample()` |
| Backdrop | Sky dome `ShaderMaterial` | `evaluateStudioBackdrop()` |
| Anti-aliasing | 4× MSAA on the HDR target | Jittered sub-pixel samples, accumulated |
| Tone pipeline | `StudioCameraShader` (last `ShaderPass`) | `FS_DISPLAY` (**identical GLSL**) |
| Hand-off helpers | `window.studio.exportGlb()`, `window.studio.rendererParams()` | — |

---

## 3. Shared conventions (do not break these)

Both pages already follow these rules. Every porting step assumes them.

1. **Right-handed, Y-up world.** Screen-right is `cross(forward, worldUp)`, which
   matches `THREE.PerspectiveCamera`. The vanilla renderers use the same basis; a
   left-handed basis would render the scene mirrored.
2. **Normalized studio space.** All content is centered over the origin in X/Z,
   rests on `y = 0`, and is scaled so its bounding sphere (centered at half-height)
   has radius 1. `threejs.html` applies this transform to the `studioRoot` group,
   and `exportGlb()` bakes it into the export. Re-normalizing in `renderer.html` is
   therefore an identity, so **camera coordinates from `rendererParams()` are valid
   in `renderer.html` as-is**.
3. **Linear color everywhere.**
   - Studio constants and material colors are linear sRGB.
   - In three.js, build colors with `new THREE.Color().setRGB(r, g, b)`, which is
     linear in r152+. Don't use hex or CSS strings for values you'll port by
     hand; three treats those as sRGB-encoded.
   - glTF `COLOR_0` and `baseColorFactor` are linear by spec.
4. **Light units mean the same thing.**
   - A `DirectionalLight` whose `color × intensity` equals `SUN_RADIANCE` gives
     exactly `albedo/π · N·L · SUN_RADIANCE`.
   - The `HemisphereLight` colors are `π × skyAvg` and `π × groundBounce`, because
     three divides by π and `skyAmbient()` is already divided by π.
   - Edit the constants, not the light objects ([step 6](#step-6--port-changed-studio-constants)).
5. **The tone pipeline is shared code.** `StudioCameraShader` and `FS_DISPLAY` are
   the same GLSL. If you change one (exposure, vignette, grain), paste the change
   into the other.
6. **Every mesh is double-sided and opaque.** `renderer.html` flips the shading
   normal toward the viewer, and `threejs.html` uses `THREE.DoubleSide`.

---

## 4. Feature support matrix

`applyStudioMaterials()` replaces every material in `threejs.html` with a
`MeshStandardMaterial` limited to what `renderer.html` can draw. If something looks
right in `threejs.html`, it will port. If you work around that function, use this
table:

| three.js feature | `renderer.html` | What to do |
| :--- | :--- | :--- |
| `Mesh` + `BufferGeometry` (indexed or not), nested groups, any TRS or matrix | ✅ | Export (step 4) |
| `material.color` | ✅ `baseColorFactor` | — |
| Vertex colors (`geometry.attributes.color`) | ✅ `COLOR_0` × `baseColorFactor` | — |
| `roughness`, `metalness` per material | ⚠️ ignored by default; studio `MESH_ROUGHNESS`, dielectric | Set `STUDIO_MATERIAL_OVERRIDE = false` and apply [step 5](#step-5--port-per-material-roughness--metalness-optional) |
| Primitive with no glTF material | ✅ grey `MESH_DEFAULT_ALBEDO` (or vertex colors × 1), studio roughness | — |
| `map` or any texture | ❌ ignored (console warning in both pages) | Bake into vertex colors before export |
| `emissive`, `opacity`, `transparent`, `transmission`, clearcoat, sheen, … | ❌ renders as opaque standard | Not portable without extending `FS_TRACE` (see [§9](#9-appendix-rendererhtml-scene-data-layout)) |
| `MeshBasic`, `MeshLambert` or `MeshPhong` material | ⚠️ replaced by standard (color kept) | — |
| `InstancedMesh` | ❌ `EXT_mesh_gpu_instancing` is ignored by `parseGlb()` | Expand to plain meshes before export (snippet in step 1) |
| `SkinnedMesh`, morph targets | ⚠️ bind pose only | Avoid, or bake the pose into a new `BufferGeometry` |
| `Points`, `Line`, `Sprite` | ❌ | — |
| Extra lights (`PointLight`, `SpotLight`, …) | ❌ only the studio sun + sky | Change the studio constants instead |
| Arbitrary `PerspectiveCamera` | ✅ via a locked camera ([step 7](#step-7--lock-the-camera-andor-sun-optional)) | — |
| `OrthographicCamera` | ❌ | — |
| Draco/Meshopt-compressed source `.glb` | ❌ in `renderer.html` | `exportGlb()` always writes uncompressed data |

---

## 5. The porting procedure

### Step 1 — Author the scene in `threejs.html`

Put **all** additions in `buildSceneContent(content)` (§2 of `threejs.html`).
Objects added there:

- are normalized together with the loaded model, so author them in the model's
  native units (the elephant spans X ±0.69, Y 0–2.44, Z −1.66–2.13);
- get studio materials, shadows and AO;
- are included in `exportGlb()`.

```js
function buildSceneContent(content) {
  const box = new THREE.Mesh(
    new THREE.BoxGeometry(0.9, 0.9, 0.9),
    new THREE.MeshStandardMaterial({ color: new THREE.Color().setRGB(0.55, 0.08, 0.05), roughness: 0.8 })
  );
  box.position.set(-1.6, 0.45, -0.6);
  content.add(box);
}
```

Rules:

- **Use `MeshStandardMaterial`** with `color`, `roughness` and `metalness` only.
- **Build colors with `setRGB()`** (linear). See [§3](#3-shared-conventions-do-not-break-these).
- **Don't add lights, change the camera, or touch `scene`/`studioRoot` directly.**
  Lights and camera come from the constants, and anything outside `content` is not
  exported.
- **To render your own model without the elephant,** load it with
  `?model=../common/files/yours.glb` (or drop it on the frame) and leave
  `buildSceneContent` empty. `content` is a child of the same group, so both routes
  are handled the same way.
- **Expand `InstancedMesh` before export:**

  ```js
  function expandInstances(inst) {
    const group = new THREE.Group();
    const m = new THREE.Matrix4();
    for (let i = 0; i < inst.count; i++) {
      inst.getMatrixAt(i, m);
      const mesh = new THREE.Mesh(inst.geometry, inst.material);
      m.decompose(mesh.position, mesh.quaternion, mesh.scale);
      group.add(mesh);
    }
    return group;
  }
  ```

Reload `threejs.html` and iterate until the preview looks right.

### Step 2 — Choose the material policy

At the top of `threejs.html`:

```js
const STUDIO_MATERIAL_OVERRIDE = true;
```

| Setting | `threejs.html` preview | `renderer.html` work |
| :--- | :--- | :--- |
| `true` (default) | Every material is forced to `MESH_ROUGHNESS` and dielectric; only colors vary | None. The stock renderer matches. |
| `false` | Each authored material keeps its `roughness`/`metalness`. Primitives with no glTF material still get studio values. | Apply [step 5](#step-5--port-per-material-roughness--metalness-optional) |

### Step 3 — Freeze the view and read the hand-off parameters

Orbit `threejs.html` to the composition you want (or open it with `?yaw=…&pitch=…`),
then run this in the DevTools console:

```js
studio.rendererParams()
// {
//   query:   "?yaw=-120.00&pitch=14.00",
//   camera:  { pos: [...], forward: [...], right: [...], up: [...], tanHalfV: 0.267949 },
//   sunDir:  [0.613347, 0.71934, -0.326122]
// }
```

- **`query`** is all you need when you keep auto-framing: the same yaw and pitch
  frame identically at the same aspect ratio in both pages.
- **`camera` and `sunDir`** are for locking the exact view
  ([step 7](#step-7--lock-the-camera-andor-sun-optional)).

### Step 4 — Export the geometry

In the same console:

```js
await studio.exportGlb("my-scene.glb")   // downloads my-scene.glb
```

Move the file into `common/files/`. The export contains:

- everything under `studioRoot` (the loaded model plus `buildSceneContent`
  additions), in normalized studio space;
- each mesh's `color` as `baseColorFactor`, plus `roughnessFactor` and
  `metallicFactor`;
- vertex colors as `COLOR_0`.

It is uncompressed, so `renderer.html`'s `parseGlb()` reads it directly.

> `exportGlb()` returns the `ArrayBuffer` too, which is handy in automation (see [step 9](#step-9--verify-with-an-image-diff)).

### Step 5 — Port per-material roughness & metalness (optional)

Skip this step if `STUDIO_MATERIAL_OVERRIDE` is `true`.

The stock `renderer.html` uses one roughness (`MESH_ROUGHNESS`) and one dielectric
F0 (`MESH_F0`) for every triangle. The edits below carry each glTF material's
`roughnessFactor` and `metallicFactor` per triangle. They are packed into the
spare `.w` channels of the attribute texture, so no new textures or uniforms are
needed, and they switch the shading to the metallic workflow three.js uses.

> **Defaults.** glTF defaults both factors to **1**, and GLTFLoader follows the
> spec. Primitives with **no material** keep the studio values, so the stock
> elephant renders byte-identically with or without this patch. This was verified:
> the mean difference was 0.0.

Make these ten edits in `renderer.html`, in order. Each "find" string occurs
exactly once.

**5.1 — JS constants** (top of the IIFE): add a JS copy of the GLSL constant.

```js
// find
  const MESH_DEFAULT_ALBEDO = [0.18, 0.18, 0.18];
// replace with
  const MESH_DEFAULT_ALBEDO = [0.18, 0.18, 0.18];
  const MESH_ROUGHNESS = 0.58;
```

**5.2 — `parseGlb()`**: add a per-triangle material array.

```js
// find
    const outC = [];
// replace with
    const outC = [];
    const outM = [];
```

**5.3 — `parseGlb()` → `emitPrimitive()`**: read the factors.

```js
// find
      if (pbr && pbr.baseColorTexture) console.warn("glTF base color textures are ignored; using baseColorFactor");
// replace with
      if (pbr && pbr.baseColorTexture) console.warn("glTF base color textures are ignored; using baseColorFactor");
      // glTF defaults both factors to 1; primitives without a material keep the studio values
      const roughness = mat ? (pbr && pbr.roughnessFactor !== undefined ? pbr.roughnessFactor : 1.0) : MESH_ROUGHNESS;
      const metallic = mat ? (pbr && pbr.metallicFactor !== undefined ? pbr.metallicFactor : 1.0) : 0.0;
```

**5.4 — `emitPrimitive()` triangle loop** (the second `for (let t = 0; …)` loop,
the one followed by `for (let k = 0; k < 3; k++) {`):

```js
// find
      for (let t = 0; t + 2 < idx.length; t += 3) {
        for (let k = 0; k < 3; k++) {
// replace with
      for (let t = 0; t + 2 < idx.length; t += 3) {
        outM.push(roughness, metallic);
        for (let k = 0; k < 3; k++) {
```

**5.5 — `parseGlb()` return value**:

```js
// find
      colors: new Float32Array(outC)
    };
// replace with
      colors: new Float32Array(outC),
      materials: new Float32Array(outM)
    };
```

**5.6 — `buildBvhTextures()`**: pack roughness into `n0.w` and metallic into `n1.w`.

```js
// find
        attrTex.data.set([...mesh.normals.subarray(t * 9 + i * 3, t * 9 + i * 3 + 3), 0], k * 24 + i * 4);
// replace with
        // .w of the normal texels: roughness on n0, metallic on n1
        const pbrW = i < 2 ? mesh.materials[t * 2 + i] : 0;
        attrTex.data.set([...mesh.normals.subarray(t * 9 + i * 3, t * 9 + i * 3 + 3), pbrW], k * 24 + i * 4);
```

`t` is the **original** triangle index here, and `k` is the BVH-sorted index, so
the material follows its triangle through the BVH reordering. Don't index
`materials` with `k`.

**5.7 — `FS_TRACE` `struct SurfHit`**:

```glsl
// find
    float roughness;
    float f0;
  };
// replace with
    float roughness;
    float f0;
    float metallic;
  };
```

**5.8 — `traceScene()` ground defaults**:

```glsl
// find
    s.f0 = 0.03;
// replace with
    s.f0 = 0.03;
    s.metallic = 0.0;
```

**5.9 — `traceScene()` mesh branch**: read the packed values.

```glsl
// find
      s.roughness = MESH_ROUGHNESS;
      s.f0 = MESH_F0;
// replace with
      s.roughness = fetchTexel(u_attrTex, ti * 6).w;
      s.metallic = fetchTexel(u_attrTex, ti * 6 + 1).w;
      s.f0 = MESH_F0;
```

**5.10 — `renderSample()`**: switch to the metallic workflow.

```glsl
// find
    vec3 f0 = vec3(s.f0);
    vec3 direct = cookTorranceBrdf(s.n, v, u_sunDir, s.albedo, s.roughness, f0) * SUN_RADIANCE * sunVis;
    vec3 fres = schlickFresnel(saturate(dot(s.n, v)), f0);
    vec3 ambient = s.albedo * skyAmbient(s.n) * (1.0 - fres) * ao;
// replace with
    // Metallic workflow: metals tint F0 with the base color and have no diffuse lobe
    vec3 f0 = mix(vec3(s.f0), s.albedo, s.metallic);
    vec3 diffuseAlbedo = s.albedo * (1.0 - s.metallic);
    vec3 direct = cookTorranceBrdf(s.n, v, u_sunDir, diffuseAlbedo, s.roughness, f0) * SUN_RADIANCE * sunVis;
    vec3 fres = schlickFresnel(saturate(dot(s.n, v)), f0);
    vec3 ambient = diffuseAlbedo * skyAmbient(s.n) * (1.0 - fres) * ao;
```

The environment-specular lines that follow (`dfgApprox`, `envDir`, `envSpec`)
already use `f0` and `s.roughness`, so metals pick up the backdrop reflection
automatically.

### Step 6 — Port changed studio constants

If you edited any §1 constant in `threejs.html`, copy the value to every location
listed below.

> **Watch out:** `renderer.html` keeps the light, sky and ground values as **GLSL
> `const`s inside the `FS_TRACE` template string**, not as JS. For example, JS
> `[3.55, 3.20, 2.70]` becomes GLSL `vec3(3.55, 3.20, 2.70)`. `renderer.py` uses the
> same names if you want the Python renderer to match too.

| Constant | `threejs.html` | `renderer.html` | `python/renderer.py` |
| :--- | :--- | :--- | :--- |
| `CAM_VFOV_DEG`, `CAM_FRAME_FILL` | §1 JS | JS const | module const |
| `SUN_AZIMUTH_REL_DEG`, `SUN_ELEVATION_DEG` | §1 JS | JS const | module const |
| `MESH_DEFAULT_ALBEDO` | §1 JS | JS const | module const (NumPy array) |
| Default yaw / pitch | `urlParams.get("yaw") ?? "-142"`, `… "pitch") ?? "11"` | same expressions | `RENDER_YAW` / `RENDER_PITCH` env defaults |
| `SUN_RADIANCE`, `SKY_ZENITH_RAD`, `SKY_HORIZON_RAD`, `GROUND_ALBEDO` | §1 JS arrays | `FS_TRACE` `const vec3` | module const |
| `GROUND_ROUGHNESS`, `GROUND_FADE_START`, `GROUND_FADE_END`, `MESH_ROUGHNESS`, `AO_MAX_DIST` | §1 JS | `FS_TRACE` `const float` (and the JS `MESH_ROUGHNESS` from step 5.1, if added) | module const |
| Sun softness | `sun.shadow.radius = 9` (VSM blur, tuned by eye) | `SUN_TAN_RADIUS = tan(angle)` (default `tan(2.2°) = 0.038416`) | `SUN_ANGULAR_RADIUS` |
| Dielectric F0 | fixed at 0.04 by three.js | `MESH_F0 = 0.035`, ground `0.03` | `MESH_F0` |
| Exposure, vignette, grain | `StudioCameraShader` | `FS_DISPLAY` | `apply_camera_optics_and_aces()` |

**Sun softness** has no exact three.js equivalent. If you change the angular radius,
scale `sun.shadow.radius` roughly in proportion (9 ↔ 2.2°) so the preview stays
representative.

**If you changed the light objects instead of the constants**, convert back:

```js
// SUN_RADIANCE from a DirectionalLight
sun.color.clone().multiplyScalar(sun.intensity).toArray()
// skyAvg from a HemisphereLight; renderer.html derives the ground bounce itself,
// so only the sky side is portable:
hemi.color.clone().multiplyScalar(hemi.intensity / Math.PI).toArray()   // = (SKY_ZENITH_RAD + SKY_HORIZON_RAD) / 2
```

### Step 7 — Lock the camera and/or sun (optional)

With auto-framing, the yaw/pitch `query` reproduces the view **at the same aspect
ratio**. If you need the exact camera at any frame size, or the sun at a fixed
world direction instead of following the camera yaw, paste `rendererParams()`
output into `renderer.html`:

```js
// 7.1 — next to the other JS constants (e.g. just above `const MAX_SPP = 256;`)
  // Locked view pasted from threejs.html: studio.rendererParams()
  const FIXED_CAMERA = { pos: [2.474079, 1.025129, 1.498598], forward: [-0.840301, -0.241922, -0.485148], right: [0.5, 0, -0.866025], up: [-0.209511, 0.970296, -0.120961], tanHalfV: 0.267949 };
  const FIXED_SUN_DIR = [0.613347, 0.71934, -0.326122];
```

```js
// 7.2 — resetAccumulation()
// find
    if (mesh) cam = frameCameraToMesh(mesh.positions, bufW / Math.max(1, bufH));
// replace with
    if (mesh) cam = FIXED_CAMERA || frameCameraToMesh(mesh.positions, bufW / Math.max(1, bufH));
```

```js
// 7.3 — sunDirection()
// find
  function sunDirection() {
// replace with
  function sunDirection() {
    if (FIXED_SUN_DIR) return FIXED_SUN_DIR;
```

Set either constant to `null` to fall back to the orbit behavior. A locked camera
ignores orbit drags. `tanHalfV` is the vertical half-FOV tangent; the horizontal
extent still follows the canvas aspect, exactly as three.js does.

### Step 8 — Point `renderer.html` at the scene

- **Ad hoc:** `renderer.html?model=../common/files/my-scene.glb&yaw=-120&pitch=14`,
  i.e. the `query` from step 3 plus `&model=…`.
- **Permanent:** edit `DEFAULT_MODEL_URL` and the `yaw`/`pitch` defaults in
  `renderer.html`.

Let it converge to 256 samples per pixel; the caption reads
`… · 256/256 spp`. `window.paintDone` becomes `true` at that point.

### Step 9 — Verify with an image diff

Compare the two canvases at the same viewport. Both pages expose
`window.paintDone` and use `preserveDrawingBuffer`, so `toDataURL()` captures
exactly what is shown.

Save this as `compare_ports.py` anywhere **outside** the repo, then run it while
the server from §1 is up:

```python
#!/usr/bin/env python3
"""Diff threejs.html against renderer.html: python3 compare_ports.py BASE_URL QUERY OUT.png"""
import asyncio, base64, io, sys

import numpy as np
from PIL import Image
from playwright.async_api import async_playwright

base, query, out = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3]


async def grab(browser, page):
    pg = await browser.new_page(viewport={"width": 1280, "height": 900})
    pg.on("pageerror", lambda e: print(page, "pageerror:", e))
    await pg.goto(f"{base}/javascript/{page}{query}")
    await pg.wait_for_function("window.paintDone === true", timeout=120000)
    data = await pg.evaluate("document.getElementById('c').toDataURL('image/png')")
    return Image.open(io.BytesIO(base64.b64decode(data.split(",")[1]))).convert("RGB")


async def main():
    async with async_playwright() as p:
        # channel="chrome" uses the installed Chrome (real GPU); drop it to use Playwright's Chromium
        browser = await p.chromium.launch(channel="chrome", headless=True, args=["--ignore-gpu-blocklist"])
        ref = await grab(browser, "renderer.html")
        three = await grab(browser, "threejs.html")
        await browser.close()
    a = np.asarray(ref, dtype=float)
    b = np.asarray(three, dtype=float)
    d = np.abs(a - b).mean(axis=-1)
    print(f"mean abs diff {d.mean():.2f}/255   p95 {np.percentile(d, 95):.1f}/255")
    w, h = ref.size
    sheet = Image.new("RGB", (w, h * 2))
    sheet.paste(ref, (0, 0))
    sheet.paste(three, (0, h))
    sheet.save(out)  # top: renderer.html, bottom: threejs.html


asyncio.run(main())
```

```sh
python3 compare_ports.py http://127.0.0.1:8080 \
  "?model=../common/files/my-scene.glb&yaw=-120&pitch=14" /tmp/port_check.png
```

To compare **your exported scene in both pages**, pass `model=` in the query, so
`threejs.html` loads the same `.glb`. Leave `buildSceneContent` empty while you do
this, or the additions are added a second time.

Acceptance thresholds, measured on Apple M2 Max / Chrome / ANGLE-Metal at
1118 × 544:

| Scene | Mean abs diff | p95 |
| :--- | :--- | :--- |
| Stock elephant, default view | 6.4 / 255 | 16 / 255 |
| Worked example (§7), `STUDIO_MATERIAL_OVERRIDE = false` + step 5 | 7.2 / 255 | 17 / 255 |

A correct port should land in the same range. **Above about 12/255, look at the
sheet** and check [§8](#8-troubleshooting); a structural mistake usually shows up
immediately.

---

## 6. Expected residual differences

These are inherent to rasterizing vs. path tracing. They are not porting errors.

| Effect | `threejs.html` | `renderer.html` | Visible as |
| :--- | :--- | :--- | :--- |
| Diffuse model | Lambert | Oren-Nayar (roughness-dependent flattening) | three slightly darker toward silhouettes and the terminator |
| Sun shadow | VSM, constant blur | Ray-traced disc, penumbra grows with distance | Contact shadows softer in three; distant shadow edges sharper |
| Ambient occlusion | GTAO, screen-space (misses off-screen occluders) | Ray-traced, `AO_MAX_DIST` | Different crease and contact darkening |
| Sky specular | Prefiltered PMREM × three's DFG + multiscatter compensation off | Unfiltered gradient lookup × the same DFG approximation | Nearly identical (the backdrop is a smooth gradient) |
| Indirect diffuse Fresnel | None | `ambient × (1 − F)` | ≤4% at grazing angles |
| Dielectric F0 | 0.04 | 0.035 mesh / 0.03 ground | Negligible |
| Shadow terminator | Shadow-map bias | Hanika (2021) origin lift | Slight differences at low-poly silhouettes |
| Anti-aliasing | 4× MSAA | 256-sample jitter | Edge rendering |
| Ground | Disc of radius `GROUND_FADE_END + 0.5` | Infinite plane | Identical after the fade |

---

## 7. Worked example

This is the dry run used to validate this guide. It adds a gold metal sphere and a
rough red box next to the elephant, keeps their materials, exports, ports and
verifies.

1. **Author** (`threejs.html`):

   ```js
   const STUDIO_MATERIAL_OVERRIDE = false;
   ```

   ```js
   function buildSceneContent(content) {
     const gold = new THREE.Mesh(
       new THREE.SphereGeometry(0.45, 64, 32),
       new THREE.MeshStandardMaterial({ color: new THREE.Color().setRGB(1.0, 0.77, 0.34), metalness: 1.0, roughness: 0.25 })
     );
     gold.position.set(1.5, 0.45, 1.4);
     const box = new THREE.Mesh(
       new THREE.BoxGeometry(0.9, 0.9, 0.9),
       new THREE.MeshStandardMaterial({ color: new THREE.Color().setRGB(0.55, 0.08, 0.05), metalness: 0.0, roughness: 0.8 })
     );
     box.position.set(-1.6, 0.45, -0.6);
     box.rotation.y = 0.5;
     content.add(gold, box);
   }
   ```

2. **Freeze the view**: open `threejs.html?yaw=-120&pitch=14`, then:

   ```js
   studio.rendererParams().query   // "?yaw=-120.00&pitch=14.00"
   ```

3. **Export**: `await studio.exportGlb("scene.glb")`, then move the file to
   `common/files/scene.glb`. The result is 213 KB, 8,420 triangles.

4. **Port materials**: apply step 5.1–5.10 to `renderer.html`.

5. **Load**: open
   `renderer.html?yaw=-120.00&pitch=14.00&model=../common/files/scene.glb`.

6. **Verify**: the diff against `threejs.html?yaw=-120&pitch=14` (the authored
   page) was **7.22/255 mean, 17.3 p95**. Locking the view with step 7 and loading
   without a `yaw` query gave the same 7.22/255. Both pages show the gold sphere as
   a metal, reflecting the backdrop and casting a shadow, with the red box behind
   the elephant.

---

## 8. Troubleshooting

| Symptom in `renderer.html` | Cause | Fix |
| :--- | :--- | :--- |
| Image is a mirror of `threejs.html` | Left-handed camera basis (a reverted or hand-written `buildCameraBasis()`) | `right = cross(forward, worldUp)`, `up = cross(right, forward)` |
| "Couldn't load …" message | Opened via `file://`, or wrong `model` path | Serve the repo root (§1); `model` is relative to `javascript/` |
| Different framing at the same yaw/pitch | Different canvas aspect ratio | Compare at the same viewport, or lock the camera (step 7) |
| Everything looks metallic or mirror-like | Step 5 applied, but your materials rely on glTF's default factors (1) | Set `roughness`/`metalness` explicitly in three.js |
| Metals are dark, or dielectric-looking | Step 5 not applied, or `STUDIO_MATERIAL_OVERRIDE` still `true` when exporting | Apply step 5 and re-export with the override off |
| Colors washed out or too dark vs. the preview | A hand-ported color was taken from a hex/CSS value (sRGB) instead of linear | Use `setRGB()` in three.js, or convert with `color.convertSRGBToLinear()` |
| Objects missing | They were outside `studioRoot` (added to `scene`), or were `InstancedMesh`/`Points`/`Line` | Add them in `buildSceneContent`; expand instances (step 1) |
| Ground shows a horizon line | `GROUND_FADE_*` or `SKY_HORIZON_RAD` changed in only one file | Copy all ground and sky constants (step 6) |
| Exposure or contrast differs everywhere | Tone pipeline edited in only one file | Keep `StudioCameraShader` and `FS_DISPLAY` identical |
| Very slow convergence on big scenes | Large triangle counts make each sample cost more | Expected. `MAX_SPP` and `MAX_BUFFER_PIXELS` trade quality for time. |

---

## 9. Appendix: `renderer.html` scene data layout

Use this when extending `renderer.html` beyond step 5, for example for emissive
color or a second light.

- **Triangle soup.** `parseGlb()` returns `positions`, `normals` and `colors`
  (9 floats per triangle each, world space, linear color), plus `materials`
  (2 per triangle) after step 5.
- **Textures** (`RGBA32F`, 2048 texels wide, texel `i` at `(i & 2047, i >> 11)`):

  | Texture | Texels per item | Layout |
  | :--- | :--- | :--- |
  | `u_nodeTex` | 2 per BVH node | `[bmin.xyz, leaf ? triStart : rightChild]`, `[bmax.xyz, leaf ? triCount : -(axis + 1)]`; left child is always `node + 1` |
  | `u_triTex` | 3 per triangle | `v0.xyz`, `e1.xyz`, `e2.xyz` (Möller–Trumbore form) |
  | `u_attrTex` | 6 per triangle | `n0, n1, n2` (vertex normals; `.w` is free; step 5 uses `n0.w`, `n1.w`), `c0, c1, c2` (vertex colors; `.w` is free) |

  Triangles are stored in BVH order. When packing, index source arrays with the
  original triangle `t` and write at the sorted slot `k`, as `buildBvhTextures()`
  does.
- **Spare channels.** `n2.w`, `c0.w`, `c1.w` and `c2.w` are still free. That is
  room for, e.g., an emissive RGB plus intensity without touching the BVH. Read
  them in `traceScene()` and add the term in `renderSample()`. Mirror any new
  material feature in `applyStudioMaterials()`, or the preview stops being
  faithful.
- **Lights.** The sun is one direction (`u_sunDir`) plus `SUN_RADIANCE`, sampled
  with one shadow ray per pass. A second light means a second jittered
  `traceMesh(…, true)` visibility ray and BRDF evaluation in `renderSample()`. The
  per-frame cost scales accordingly.
