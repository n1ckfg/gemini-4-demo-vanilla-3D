#!/usr/bin/env python3
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
PLY Mesh Renderer
=================

Physically-Based Studio Ray-Tracing Engine for Triangle Meshes
--------------------------------------------------------------
A stripped-down sibling of `sunlit_library.py`: no procedural scene, no hard-coded
geometry. It loads a single PLY mesh (default: `../common/files/elephant.ply`) and
renders it on an analytic shadow-catching ground plane under a sun-and-sky rig.

- ASCII and binary (little/big-endian) PLY loader with per-vertex normals and
  colors when present; arbitrary polygons are fan-triangulated.
- Automatic Z-up -> Y-up conversion, ground snapping and unit-radius normalization.
- Median-split triangle BVH traversed with vectorized ray *packets* (each node is
  tested against only the subset of rays that survived its parent).
- Smooth-shaded Moller-Trumbore intersections with barycentric normal and color
  interpolation, and Hanika shadow-terminator offsets for low-poly silhouettes.
- Cook-Torrance GGX + Oren-Nayar shading, soft solar-disc shadows, cosine-weighted
  ambient occlusion, depth/normal-aware bilateral denoising, edge supersampling,
  and the same ACES filmic camera pipeline as the library engine.
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image

# ==============================================================================
# 1. MATHEMATICAL & MICROFACET BRDF UTILITY FUNCTIONS
# ==============================================================================

EPSILON: float = 1e-6
PI: float = math.pi


def saturate(x: np.ndarray | float) -> np.ndarray | float:
    """Clamp array or scalar to [0.0, 1.0]."""
    return np.clip(x, 0.0, 1.0)


def smoothstep(edge0: np.ndarray | float, edge1: np.ndarray | float, x: np.ndarray | float) -> np.ndarray:
    """Hermite C1 smooth interpolation on [edge0, edge1]."""
    t = np.clip((x - edge0) / np.maximum(edge1 - edge0, 1e-8), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def normalize(v: np.ndarray) -> np.ndarray:
    """Fast vector normalization along the last axis."""
    norm = np.sqrt(np.maximum(np.sum(v * v, axis=-1, keepdims=True), 1e-24))
    return v / norm


def dot(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Dot product along the last axis."""
    return np.sum(a * b, axis=-1)


def dot_keep(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Dot product along the last axis with keepdims=True."""
    return np.sum(a * b, axis=-1, keepdims=True)


def reflect(incident: np.ndarray, normal: np.ndarray) -> np.ndarray:
    """Reflect incident vector d around unit surface normal n."""
    return incident - 2.0 * dot_keep(incident, normal) * normal


def schlick_fresnel(cos_theta: np.ndarray, f0: np.ndarray | float) -> np.ndarray:
    """Schlick approximation to Fresnel reflectance."""
    c = np.clip(1.0 - np.clip(cos_theta, 0.0, 1.0), 0.0, 1.0)
    c2 = c * c
    c5 = c2 * c2 * c
    if isinstance(f0, np.ndarray) and f0.ndim > cos_theta.ndim:
        c5 = c5[..., None]
    return f0 + (1.0 - f0) * c5


def ggx_isotropic_d(n_dot_h: np.ndarray, roughness: np.ndarray | float) -> np.ndarray:
    """Trowbridge-Reitz (GGX) isotropic normal distribution function D(h)."""
    alpha = np.maximum(roughness * roughness, 0.0012)
    a2 = alpha * alpha
    c2 = np.clip(n_dot_h, 0.0, 1.0) ** 2
    denom = c2 * (a2 - 1.0) + 1.0
    return a2 / np.maximum(PI * denom * denom, 1e-10)


def smith_ggx_g(n_dot_v: np.ndarray, n_dot_l: np.ndarray, roughness: np.ndarray | float) -> np.ndarray:
    """Height-correlated Smith GGX geometric shadowing-masking factor G / (4 * N.V * N.L)."""
    alpha = np.maximum(roughness * roughness, 0.0012)
    a2 = alpha * alpha
    nv = np.clip(n_dot_v, 1e-4, 1.0)
    nl = np.clip(n_dot_l, 1e-4, 1.0)
    gv = nl * np.sqrt(nv * nv * (1.0 - a2) + a2)
    gl = nv * np.sqrt(nl * nl * (1.0 - a2) + a2)
    return 0.5 / np.maximum(gv + gl, 1e-8)


def oren_nayar_diffuse(
    normal: np.ndarray,
    view_dir: np.ndarray,
    light_dir: np.ndarray,
    n_dot_l: np.ndarray,
    n_dot_v: np.ndarray,
    roughness: np.ndarray,
) -> np.ndarray:
    """
    Full Oren-Nayar rough micro-cavity diffuse reflectance factor (returns multiplier
    on Lambertian (albedo / PI) * N.L).
    """
    l_vec = light_dir[None, :] if light_dir.ndim == 1 else light_dir
    sigma2 = (roughness * 0.65) ** 2
    a_on = 1.0 - 0.5 * (sigma2 / (sigma2 + 0.33))
    b_on = 0.45 * (sigma2 / (sigma2 + 0.09))

    v_perp = view_dir - normal * n_dot_v[..., None]
    l_perp = l_vec - normal * n_dot_l[..., None]
    v_perp_norm = np.sqrt(np.maximum(dot(v_perp, v_perp), 1e-10))
    l_perp_norm = np.sqrt(np.maximum(dot(l_perp, l_perp), 1e-10))
    cos_phi_diff = np.maximum(0.0, dot(v_perp, l_perp) / (v_perp_norm * l_perp_norm))

    sin_theta_i = np.sqrt(np.maximum(0.0, 1.0 - n_dot_l * n_dot_l))
    sin_theta_r = np.sqrt(np.maximum(0.0, 1.0 - n_dot_v * n_dot_v))
    sin_alpha = np.maximum(sin_theta_i, sin_theta_r)
    tan_beta = np.minimum(sin_theta_i / np.maximum(n_dot_l, 1e-4), sin_theta_r / np.maximum(n_dot_v, 1e-4))

    return a_on + b_on * cos_phi_diff * sin_alpha * tan_beta


def cook_torrance_brdf(
    normal: np.ndarray,
    view_dir: np.ndarray,
    light_dir: np.ndarray,
    albedo: np.ndarray,
    roughness: np.ndarray,
    f0: np.ndarray,
    metallic: np.ndarray | float = 0.0,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Evaluate Cook-Torrance GGX microfacet BRDF + Oren-Nayar rough diffuse * (N . L),
    split into (diffuse, specular) radiance transfer factors.
    """
    l_vec = light_dir[None, :] if light_dir.ndim == 1 else light_dir
    h_vec = normalize(view_dir + l_vec)

    n_dot_l = np.clip(dot(normal, l_vec), 0.0, 1.0)
    n_dot_v = np.clip(dot(normal, view_dir), 1e-4, 1.0)
    n_dot_h = np.clip(dot(normal, h_vec), 0.0, 1.0)
    v_dot_h = np.clip(dot(view_dir, h_vec), 0.0, 1.0)

    d_term = ggx_isotropic_d(n_dot_h, roughness)
    g_term = smith_ggx_g(n_dot_v, n_dot_l, roughness)
    f_term = schlick_fresnel(v_dot_h, f0)

    if isinstance(metallic, np.ndarray) and metallic.ndim < f_term.ndim:
        m_exp = metallic[..., None]
    else:
        m_exp = metallic

    spec = (d_term * g_term * n_dot_l)[..., None] * f_term
    kd = (1.0 - f_term) * (1.0 - m_exp)
    on_mult = oren_nayar_diffuse(normal, view_dir, l_vec, n_dot_l, n_dot_v, roughness)
    diff = kd * (albedo / PI) * (n_dot_l * on_mult)[..., None]
    return diff, spec


def srgb_to_linear(c: np.ndarray) -> np.ndarray:
    """Decode sRGB-encoded [0, 1] color values to linear reflectance."""
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def orthonormal_basis(n: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Branchless Duff et al. (2017) tangent frame (t, b) for unit normals n (N, 3)."""
    sign = np.where(n[:, 2] >= 0.0, 1.0, -1.0)
    a = -1.0 / (sign + n[:, 2])
    b = n[:, 0] * n[:, 1] * a
    t = np.stack([1.0 + sign * n[:, 0] * n[:, 0] * a, sign * b, -sign * n[:, 0]], axis=-1)
    bt = np.stack([b, sign + n[:, 1] * n[:, 1] * a, -n[:, 1]], axis=-1)
    return t, bt


def gaussian_blur_rgb(arr: np.ndarray, radius: float) -> np.ndarray:
    """Separable 1D Gaussian blur in float64 using pure NumPy."""
    if radius <= 0.25:
        return arr
    sigma = float(radius)
    half = max(1, math.ceil(sigma * 2.6))
    xs = np.arange(-half, half + 1, dtype=np.float64)
    kernel = np.exp(-0.5 * (xs / sigma) ** 2)
    kernel /= np.sum(kernel)

    p0 = np.pad(arr, ((half, half), (0, 0), (0, 0)), mode="edge")
    out0 = np.zeros_like(arr, dtype=np.float64)
    for k, w in enumerate(kernel):
        out0 += w * p0[k : k + arr.shape[0], :, :]
    p1 = np.pad(out0, ((0, 0), (half, half), (0, 0)), mode="edge")
    out1 = np.zeros_like(arr, dtype=np.float64)
    for k, w in enumerate(kernel):
        out1 += w * p1[:, k : k + arr.shape[1], :]
    return out1


# ==============================================================================
# 2. STUDIO CAMERA, SUN/SKY & MATERIAL SPECIFICATION
# ==============================================================================
# World Coordinates (normalized units): Y-up, ground plane at Y = 0, the mesh is
# centered over the origin and scaled so its bounding sphere has radius 1.

DEFAULT_MODEL_PATH = Path(__file__).resolve().parent.parent / "common" / "files" / "elephant.ply"

# Blender's PLY exporter writes Z-up coordinates by default.
MODEL_UP_AXIS: str = os.environ.get("MODEL_UP_AXIS", "z").lower()

# Orbit camera: yaw around the vertical axis, downward pitch, vertical field of view
CAM_YAW_DEG: float = float(os.environ.get("RENDER_YAW", "142.0"))
CAM_PITCH_DEG: float = float(os.environ.get("RENDER_PITCH", "11.0"))
CAM_VFOV_DEG: float = 30.0
CAM_FRAME_FILL: float = 0.80

# Sun placed relative to the camera so the key light stays flattering at any yaw
SUN_AZIMUTH_REL_DEG: float = -58.0
SUN_ELEVATION_DEG: float = 46.0
SUN_ANGULAR_RADIUS: float = math.radians(2.2)
SUN_RADIANCE = np.array([3.55, 3.20, 2.70], dtype=np.float64)
SKY_ZENITH_RAD = np.array([0.30, 0.40, 0.58], dtype=np.float64)
SKY_HORIZON_RAD = np.array([0.66, 0.64, 0.62], dtype=np.float64)

GROUND_ALBEDO = np.array([0.46, 0.43, 0.39], dtype=np.float64)
GROUND_ROUGHNESS: float = 0.92
GROUND_FADE_START: float = 2.4
GROUND_FADE_END: float = 7.5

MESH_ROUGHNESS: float = 0.58
MESH_F0: float = 0.035
MESH_DEFAULT_ALBEDO = np.array([0.18, 0.18, 0.18], dtype=np.float64)

EXPOSURE: float = 1.0
AO_MAX_DIST: float = 0.55

# Hit kinds written into the G-buffer
KIND_MISS: int = 0
KIND_GROUND: int = 1
KIND_MESH: int = 2


# ==============================================================================
# 3. PLY MESH LOADER
# ==============================================================================

_PLY_TYPES: dict[str, str] = {
    "char": "i1", "int8": "i1", "uchar": "u1", "uint8": "u1",
    "short": "i2", "int16": "i2", "ushort": "u2", "uint16": "u2",
    "int": "i4", "int32": "i4", "uint": "u4", "uint32": "u4",
    "float": "f4", "float32": "f4", "double": "f8", "float64": "f8",
}


def _parse_ply_header(raw: bytes) -> tuple[str, list[dict], int]:
    """Parse a PLY header into (format, elements, body_offset)."""
    if not raw.startswith(b"ply"):
        raise ValueError("not a PLY file")
    end = raw.find(b"end_header")
    if end < 0:
        raise ValueError("PLY header has no end_header")
    body = raw.find(b"\n", end) + 1
    fmt = ""
    elements: list[dict] = []
    for line in raw[:end].decode("ascii", errors="replace").splitlines():
        tok = line.split()
        if not tok or tok[0] in ("comment", "obj_info"):
            continue
        if tok[0] == "format":
            fmt = tok[1]
        elif tok[0] == "element":
            elements.append({"name": tok[1], "count": int(tok[2]), "props": []})
        elif tok[0] == "property":
            if tok[1] == "list":
                elements[-1]["props"].append((tok[4], _PLY_TYPES[tok[2]], _PLY_TYPES[tok[3]]))
            else:
                elements[-1]["props"].append((tok[2], _PLY_TYPES[tok[1]], None))
    return fmt, elements, body


def _read_binary_element(raw: bytes, offset: int, elem: dict, endian: str) -> tuple[dict, int]:
    """Read one binary PLY element starting at `offset`; returns (columns, new_offset)."""
    count = elem["count"]
    props = elem["props"]
    if all(p[2] is None for p in props):
        dt = np.dtype([(name, endian + t) for name, t, _ in props])
        arr = np.frombuffer(raw, dtype=dt, count=count, offset=offset)
        return {name: arr[name] for name, _, _ in props}, offset + count * dt.itemsize

    # Fast path: a lone list property whose every row has the same length (all-quad
    # or all-triangle meshes), which maps onto a single fixed-size structured dtype.
    if len(props) == 1 and count > 0:
        name, cnt_t, item_t = props[0]
        cnt_dt = np.dtype(endian + cnt_t)
        n0 = int(np.frombuffer(raw, dtype=cnt_dt, count=1, offset=offset)[0])
        dt = np.dtype([("n", cnt_dt), ("v", endian + item_t, (n0,))])
        if offset + count * dt.itemsize <= len(raw):
            arr = np.frombuffer(raw, dtype=dt, count=count, offset=offset)
            if np.all(arr["n"] == n0):
                return {name: arr["v"]}, offset + count * dt.itemsize

    cols: dict[str, list] = {p[0]: [] for p in props}
    for _ in range(count):
        for name, t, item_t in props:
            if item_t is None:
                dt = np.dtype(endian + t)
                cols[name].append(np.frombuffer(raw, dtype=dt, count=1, offset=offset)[0])
                offset += dt.itemsize
            else:
                cnt_dt = np.dtype(endian + t)
                n = int(np.frombuffer(raw, dtype=cnt_dt, count=1, offset=offset)[0])
                offset += cnt_dt.itemsize
                item_dt = np.dtype(endian + item_t)
                cols[name].append(np.frombuffer(raw, dtype=item_dt, count=n, offset=offset))
                offset += n * item_dt.itemsize
    out = {}
    for name, t, item_t in props:
        out[name] = cols[name] if item_t is not None else np.array(cols[name], dtype=t)
    return out, offset


def _read_ascii_elements(text: str, elements: list[dict]) -> dict[str, dict]:
    """Read all elements of an ASCII PLY body."""
    tokens = text.split()
    pos = 0
    data: dict[str, dict] = {}
    for elem in elements:
        props = elem["props"]
        count = elem["count"]
        if all(p[2] is None for p in props):
            n = len(props)
            block = np.array(tokens[pos : pos + count * n], dtype=np.float64).reshape(count, n)
            pos += count * n
            data[elem["name"]] = {p[0]: block[:, i].astype(p[1]) for i, p in enumerate(props)}
            continue
        cols: dict[str, list] = {p[0]: [] for p in props}
        for _ in range(count):
            for name, t, item_t in props:
                if item_t is None:
                    cols[name].append(float(tokens[pos]))
                    pos += 1
                else:
                    n = int(tokens[pos])
                    cols[name].append(np.array(tokens[pos + 1 : pos + 1 + n], dtype=item_t))
                    pos += 1 + n
        data[elem["name"]] = {
            name: (cols[name] if item_t is not None else np.array(cols[name], dtype=t))
            for name, t, item_t in props
        }
    return data


def load_ply(path: str | Path) -> dict[str, np.ndarray]:
    """
    Load a PLY triangle/polygon mesh and return a dict with float64 `positions` (V, 3),
    `normals` (V, 3), linear `colors` (V, 3) and int64 triangle `faces` (F, 3).
    Missing normals are rebuilt from area-weighted face normals; missing colors fall
    back to a neutral grey.
    """
    raw = Path(path).read_bytes()
    fmt, elements, body = _parse_ply_header(raw)
    if fmt == "ascii":
        data = _read_ascii_elements(raw[body:].decode("ascii"), elements)
    elif fmt in ("binary_little_endian", "binary_big_endian"):
        endian = "<" if fmt == "binary_little_endian" else ">"
        data = {}
        offset = body
        for elem in elements:
            data[elem["name"]], offset = _read_binary_element(raw, offset, elem, endian)
    else:
        raise ValueError(f"unsupported PLY format: {fmt!r}")

    vert = data["vertex"]
    positions = np.stack([vert["x"], vert["y"], vert["z"]], axis=-1).astype(np.float64)

    face_cols = data.get("face", {})
    polys = face_cols.get("vertex_indices", face_cols.get("vertex_index"))
    if polys is None:
        raise ValueError("PLY has no face vertex_indices; point clouds are not supported")
    if isinstance(polys, np.ndarray):
        groups = [polys.astype(np.int64)]
    else:
        by_len: dict[int, list] = {}
        for p in polys:
            by_len.setdefault(len(p), []).append(p)
        groups = [np.array(v, dtype=np.int64) for k, v in by_len.items() if k >= 3]
    # Fan-triangulate every n-gon: (0, k, k+1) for k in [1, n-2]
    tris = [g[:, [0, k, k + 1]] for g in groups for k in range(1, g.shape[1] - 1)]
    faces = np.concatenate(tris, axis=0)

    if all(k in vert for k in ("nx", "ny", "nz")):
        normals = normalize(np.stack([vert["nx"], vert["ny"], vert["nz"]], axis=-1).astype(np.float64))
    else:
        p0, p1, p2 = positions[faces[:, 0]], positions[faces[:, 1]], positions[faces[:, 2]]
        fn = np.cross(p1 - p0, p2 - p0)
        normals = np.zeros_like(positions)
        for c in range(3):
            np.add.at(normals, faces[:, c], fn)
        normals = normalize(normals)

    color_keys = next(
        (ks for ks in (("red", "green", "blue"), ("r", "g", "b"), ("diffuse_red", "diffuse_green", "diffuse_blue"))
         if all(k in vert for k in ks)),
        None,
    )
    if color_keys is not None:
        cols = np.stack([vert[k] for k in color_keys], axis=-1)
        scale = 255.0 if np.issubdtype(cols.dtype, np.integer) else 1.0
        colors = srgb_to_linear(np.clip(cols.astype(np.float64) / scale, 0.0, 1.0))
    else:
        colors = np.broadcast_to(MESH_DEFAULT_ALBEDO, positions.shape).copy()

    return {"positions": positions, "normals": normals, "colors": colors, "faces": faces}


def normalize_mesh_to_studio(mesh: dict[str, np.ndarray], up_axis: str = MODEL_UP_AXIS) -> dict[str, np.ndarray]:
    """Rotate to Y-up, center over the origin, rest on Y = 0 and scale to unit bounding radius."""
    p = mesh["positions"]
    n = mesh["normals"]
    if up_axis == "z":
        # (x, y, z)_Zup -> (x, z, -y)_Yup keeps the frame right-handed
        p = np.stack([p[:, 0], p[:, 2], -p[:, 1]], axis=-1)
        n = np.stack([n[:, 0], n[:, 2], -n[:, 1]], axis=-1)
    elif up_axis != "y":
        raise ValueError(f"MODEL_UP_AXIS must be 'y' or 'z', got {up_axis!r}")

    lo, hi = p.min(axis=0), p.max(axis=0)
    center = 0.5 * (lo + hi)
    p = p - np.array([center[0], lo[1], center[2]])
    mid = np.array([0.0, 0.5 * (hi[1] - lo[1]), 0.0])
    radius = float(np.max(np.linalg.norm(p - mid, axis=-1)))
    p = p / max(radius, 1e-12)
    return {**mesh, "positions": p, "normals": n}


# ==============================================================================
# 4. TRIANGLE BVH & PACKET RAY TRAVERSAL
# ==============================================================================


class TriangleBVH:
    """
    Median-split bounding volume hierarchy over a triangle soup. Triangles are
    reordered so each leaf owns a contiguous range [tri_start, tri_start + tri_count).
    """

    def __init__(self, mesh: dict[str, np.ndarray], leaf_size: int = 8) -> None:
        pos = mesh["positions"]
        faces = mesh["faces"]
        tri_p = pos[faces]  # (F, 3 verts, 3 xyz)
        self.leaf_size = leaf_size
        self._tri_lo = tri_p.min(axis=1)
        self._tri_hi = tri_p.max(axis=1)
        self._centroid = tri_p.mean(axis=1)

        self.bmin: list[np.ndarray] = []
        self.bmax: list[np.ndarray] = []
        self.left: list[int] = []
        self.right: list[int] = []
        self.axis: list[int] = []
        self.tri_start: list[int] = []
        self.tri_count: list[int] = []
        self._order: list[np.ndarray] = []
        self._n_ordered = 0
        self._build(np.arange(faces.shape[0]))

        order = np.concatenate(self._order)
        self.faces = faces[order]
        self.v0 = pos[self.faces[:, 0]]
        self.e1 = pos[self.faces[:, 1]] - self.v0
        self.e2 = pos[self.faces[:, 2]] - self.v0
        self.bmin_arr = np.array(self.bmin)
        self.bmax_arr = np.array(self.bmax)

    def _build(self, idx: np.ndarray) -> int:
        node = len(self.bmin)
        self.bmin.append(self._tri_lo[idx].min(axis=0))
        self.bmax.append(self._tri_hi[idx].max(axis=0))
        self.left.append(-1)
        self.right.append(-1)
        self.axis.append(0)
        self.tri_start.append(-1)
        self.tri_count.append(0)

        if idx.size <= self.leaf_size:
            self.tri_start[node] = self._n_ordered
            self.tri_count[node] = idx.size
            self._order.append(idx)
            self._n_ordered += idx.size
            return node

        cen = self._centroid[idx]
        axis = int(np.argmax(cen.max(axis=0) - cen.min(axis=0)))
        half = idx.size // 2
        part = np.argpartition(cen[:, axis], half)
        self.axis[node] = axis
        self.left[node] = self._build(idx[part[:half]])
        self.right[node] = self._build(idx[part[half:]])
        return node

    def intersect(
        self,
        ro: np.ndarray,
        rd: np.ndarray,
        t_max: np.ndarray | float = np.inf,
        any_hit: bool = False,
        t_min: float = 1e-5,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """
        Trace rays (N, 3) against the mesh. Returns (t, tri_index, bary_u, bary_v); missed
        rays keep t = t_max and tri_index = -1. With `any_hit`, rays stop at their first
        occluder (shadow / occlusion queries).
        """
        n_rays = rd.shape[0]
        ro = np.broadcast_to(ro, rd.shape)
        t_hit = np.broadcast_to(np.asarray(t_max, dtype=np.float64), (n_rays,)).copy()
        tri_id = np.full(n_rays, -1, dtype=np.int64)
        bu = np.zeros(n_rays, dtype=np.float64)
        bv = np.zeros(n_rays, dtype=np.float64)
        with np.errstate(divide="ignore"):
            inv_d = 1.0 / rd

        stack: list[tuple[int, np.ndarray]] = [(0, np.arange(n_rays))]
        while stack:
            node, idx = stack.pop()
            if any_hit:
                idx = idx[tri_id[idx] < 0]
            if idx.size == 0:
                continue

            # Ray-slab test against this node's AABB (fmin/fmax swallow 0 * inf NaNs)
            o = ro[idx]
            inv = inv_d[idx]
            with np.errstate(invalid="ignore"):
                ta = (self.bmin_arr[node] - o) * inv
                tb = (self.bmax_arr[node] - o) * inv
            t_near = np.fmax(np.max(np.fmin(ta, tb), axis=-1), t_min)
            t_far = np.min(np.fmax(ta, tb), axis=-1)
            keep = (t_near <= t_far) & (t_near < t_hit[idx])
            idx = idx[keep]
            if idx.size == 0:
                continue

            if self.tri_count[node] > 0:
                self._intersect_leaf(node, idx, ro, rd, t_hit, tri_id, bu, bv, t_min, any_hit)
                continue

            # Push the far child first so the near child (per packet majority direction) pops next
            ax = self.axis[node]
            if float(np.mean(rd[idx, ax])) >= 0.0:
                stack.append((self.right[node], idx))
                stack.append((self.left[node], idx))
            else:
                stack.append((self.left[node], idx))
                stack.append((self.right[node], idx))

        return t_hit, tri_id, bu, bv

    def _intersect_leaf(
        self,
        node: int,
        idx: np.ndarray,
        ro: np.ndarray,
        rd: np.ndarray,
        t_hit: np.ndarray,
        tri_id: np.ndarray,
        bu: np.ndarray,
        bv: np.ndarray,
        t_min: float,
        any_hit: bool,
    ) -> None:
        """Vectorized Moller-Trumbore over a ray subset for each triangle of a leaf."""
        ox, oy, oz = ro[idx, 0], ro[idx, 1], ro[idx, 2]
        dx, dy, dz = rd[idx, 0], rd[idx, 1], rd[idx, 2]
        t_loc = t_hit[idx]
        start = self.tri_start[node]
        for k in range(start, start + self.tri_count[node]):
            e1x, e1y, e1z = self.e1[k]
            e2x, e2y, e2z = self.e2[k]
            # p = d x e2
            px = dy * e2z - dz * e2y
            py = dz * e2x - dx * e2z
            pz = dx * e2y - dy * e2x
            det = e1x * px + e1y * py + e1z * pz
            inv_det = 1.0 / np.where(np.abs(det) > 1e-14, det, 1e-14)
            sx = ox - self.v0[k, 0]
            sy = oy - self.v0[k, 1]
            sz = oz - self.v0[k, 2]
            u = (sx * px + sy * py + sz * pz) * inv_det
            # q = s x e1
            qx = sy * e1z - sz * e1y
            qy = sz * e1x - sx * e1z
            qz = sx * e1y - sy * e1x
            v = (dx * qx + dy * qy + dz * qz) * inv_det
            t = (e2x * qx + e2y * qy + e2z * qz) * inv_det
            hit = (u >= 0.0) & (v >= 0.0) & (u + v <= 1.0) & (t > t_min) & (t < t_loc)
            if not np.any(hit):
                continue
            hi = np.nonzero(hit)[0]
            t_loc[hi] = t[hi]
            g = idx[hi]
            t_hit[g] = t[hi]
            tri_id[g] = k
            bu[g] = u[hi]
            bv[g] = v[hi]


# ==============================================================================
# 5. ORBIT CAMERA & AUTOMATIC FRAMING
# ==============================================================================


def build_camera_basis() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return orthonormal (forward, right, up) basis from the orbit yaw and pitch."""
    yaw = math.radians(CAM_YAW_DEG)
    pitch = math.radians(CAM_PITCH_DEG)
    forward = np.array(
        [math.sin(yaw) * math.cos(pitch), -math.sin(pitch), math.cos(yaw) * math.cos(pitch)],
        dtype=np.float64,
    )
    right = normalize(np.cross(np.array([0.0, 1.0, 0.0]), forward))
    up = np.cross(forward, right)
    return forward, right, up


def frame_camera_to_mesh(positions: np.ndarray, aspect: float) -> np.ndarray:
    """
    Solve for a camera position that fits every vertex inside CAM_FRAME_FILL of the
    frustum and centers the projected silhouette. Returns the camera origin (3,).
    """
    forward, right, up = build_camera_basis()
    tan_v = math.tan(math.radians(CAM_VFOV_DEG) * 0.5) * CAM_FRAME_FILL
    tan_h = tan_v * aspect
    target = 0.5 * (positions.min(axis=0) + positions.max(axis=0))
    dist = 3.0
    for _ in range(4):
        rel = positions - target
        x, y, z = rel @ right, rel @ up, rel @ forward
        dist = float(np.max(np.maximum(np.abs(x) / tan_h, np.abs(y) / tan_v) - z))
        sx = x / (dist + z)
        sy = y / (dist + z)
        target = target + right * (0.5 * (sx.max() + sx.min()) * dist) + up * (0.5 * (sy.max() + sy.min()) * dist)
    return target - forward * dist


def generate_camera_rays(width: int, height: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Generate primary ray directions (H, W, 3) and screen-plane coordinates (u, v)."""
    forward, right, up = build_camera_basis()
    v_half = math.tan(math.radians(CAM_VFOV_DEG) * 0.5)
    u_half = v_half * float(width) / float(max(height, 1))
    u_1d = (np.arange(width, dtype=np.float64) + 0.5) / width * 2.0 * u_half - u_half
    v_1d = v_half - (np.arange(height, dtype=np.float64) + 0.5) / height * 2.0 * v_half
    uu, vv = np.meshgrid(u_1d, v_1d)
    dirs = forward[None, None, :] + uu[..., None] * right[None, None, :] + vv[..., None] * up[None, None, :]
    return normalize(dirs), uu, vv


def sun_direction() -> np.ndarray:
    """World-space unit vector toward the sun, orbiting with the camera yaw."""
    az = math.radians(CAM_YAW_DEG + 180.0 + SUN_AZIMUTH_REL_DEG)
    el = math.radians(SUN_ELEVATION_DEG)
    return np.array([math.sin(az) * math.cos(el), math.sin(el), math.cos(az) * math.cos(el)])


# ==============================================================================
# 6. SCENE TRACING & G-BUFFER
# ==============================================================================


def trace_scene(bvh: TriangleBVH, mesh: dict[str, np.ndarray], ro: np.ndarray, rd: np.ndarray) -> dict[str, np.ndarray]:
    """
    Trace rays against the mesh and the Y = 0 ground plane. Returns a flat G-buffer
    with `t`, `kind`, `pos`, shading `normal`, geometric `normal_geo`, `albedo`,
    `roughness`, `f0` and a terminator-safe secondary-ray origin `pos_offset`.
    """
    n = rd.shape[0]
    ro_b = np.broadcast_to(ro, rd.shape)
    with np.errstate(divide="ignore", invalid="ignore"):
        t_ground = np.where((rd[:, 1] < -1e-9) & (ro_b[:, 1] > 0.0), -ro_b[:, 1] / rd[:, 1], np.inf)

    t_mesh, tri, bu, bv = bvh.intersect(ro, rd, t_max=t_ground)
    is_mesh = tri >= 0
    is_ground = ~is_mesh & np.isfinite(t_ground)

    t = np.where(is_mesh, t_mesh, t_ground)
    kind = np.full(n, KIND_MISS, dtype=np.int8)
    kind[is_ground] = KIND_GROUND
    kind[is_mesh] = KIND_MESH

    pos = np.zeros((n, 3))
    normal = np.zeros((n, 3))
    normal[:, 1] = 1.0
    normal_geo = normal.copy()
    albedo = np.zeros((n, 3))
    roughness = np.full(n, GROUND_ROUGHNESS)
    f0 = np.full(n, 0.03)

    gi = np.nonzero(is_ground)[0]
    pos[gi] = ro_b[gi] + rd[gi] * t[gi, None]
    pos[gi, 1] = 0.0
    albedo[gi] = GROUND_ALBEDO
    pos_offset = pos.copy()
    pos_offset[gi, 1] = 1e-4

    mi = np.nonzero(is_mesh)[0]
    if mi.size:
        f = bvh.faces[tri[mi]]
        w1 = bu[mi][:, None]
        w2 = bv[mi][:, None]
        w0 = 1.0 - w1 - w2
        d = rd[mi]
        p = ro_b[mi] + d * t[mi, None]

        ng = normalize(np.cross(bvh.e1[tri[mi]], bvh.e2[tri[mi]]))
        nv0, nv1, nv2 = mesh["normals"][f[:, 0]], mesh["normals"][f[:, 1]], mesh["normals"][f[:, 2]]
        ns = normalize(w0 * nv0 + w1 * nv1 + w2 * nv2)
        # Face the shading normal toward the viewer, then the geometric normal toward it
        ns = np.where(dot_keep(ns, d) > 0.0, -ns, ns)
        ng = np.where(dot_keep(ng, ns) < 0.0, -ng, ng)

        # Hanika (2021) shadow-terminator fix: lift the origin onto the curved surface
        # implied by the vertex normals so low-poly facets don't self-shadow.
        pv = mesh["positions"]
        lift = np.zeros_like(p)
        for w, vi, nvi in ((w0, f[:, 0], nv0), (w1, f[:, 1], nv1), (w2, f[:, 2], nv2)):
            nvi = np.where(dot_keep(nvi, ns) < 0.0, -nvi, nvi)
            dd = np.minimum(dot_keep(p - pv[vi], nvi), 0.0)
            lift -= w * dd * nvi

        pos[mi] = p
        normal[mi] = ns
        normal_geo[mi] = ng
        pos_offset[mi] = p + lift + ng * 2e-4
        cols = mesh["colors"]
        albedo[mi] = w0 * cols[f[:, 0]] + w1 * cols[f[:, 1]] + w2 * cols[f[:, 2]]
        roughness[mi] = MESH_ROUGHNESS
        f0[mi] = MESH_F0

    return {
        "t": t,
        "kind": kind,
        "pos": pos,
        "pos_offset": pos_offset,
        "normal": normal,
        "normal_geo": normal_geo,
        "albedo": albedo,
        "roughness": roughness,
        "f0": f0,
    }


# ==============================================================================
# 7. SOFT SUN SHADOWS & AMBIENT OCCLUSION
# ==============================================================================


def compute_visibility(
    hit: dict[str, np.ndarray],
    bvh: TriangleBVH,
    rng: np.random.Generator,
    sun_samples: int = 4,
    ao_samples: int = 10,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Monte Carlo sun visibility (rays jittered across the solar disc) and cosine-weighted
    ambient occlusion with smooth distance falloff, for every opaque G-buffer sample.
    """
    n = hit["t"].shape[0]
    sun_vis = np.ones(n)
    ao = np.ones(n)
    opaque = np.nonzero(hit["kind"] != KIND_MISS)[0]
    if opaque.size == 0:
        return sun_vis, ao

    origin = hit["pos_offset"][opaque]
    ns = hit["normal"][opaque]
    ng = hit["normal_geo"][opaque]
    m = opaque.size

    l_sun = sun_direction()
    st, sb = orthonormal_basis(l_sun[None, :])
    lit = (dot(ns, l_sun[None, :]) > 0.0) & (dot(ng, l_sun[None, :]) > -0.2)
    li = np.nonzero(lit)[0]
    acc = np.zeros(m)
    if li.size:
        for _ in range(sun_samples):
            r = np.sqrt(rng.random(li.size)) * math.tan(SUN_ANGULAR_RADIUS)
            phi = rng.random(li.size) * 2.0 * PI
            dirs = normalize(
                l_sun[None, :] + (r * np.cos(phi))[:, None] * st + (r * np.sin(phi))[:, None] * sb
            )
            _, tri, _, _ = bvh.intersect(origin[li], dirs, any_hit=True)
            acc[li] += (tri < 0).astype(np.float64)
        sun_vis[opaque] = acc / sun_samples
    sun_vis[opaque[~lit]] = 0.0

    tb, bb = orthonormal_basis(ns)
    acc = np.zeros(m)
    for _ in range(ao_samples):
        r1 = rng.random(m)
        r2 = rng.random(m)
        rad = np.sqrt(r1)
        phi = 2.0 * PI * r2
        z = np.sqrt(np.maximum(0.0, 1.0 - r1))
        dirs = (rad * np.cos(phi))[:, None] * tb + (rad * np.sin(phi))[:, None] * bb + z[:, None] * ns
        t_occ, _, _, _ = bvh.intersect(origin, dirs, t_max=AO_MAX_DIST)
        acc += smoothstep(0.0, AO_MAX_DIST, t_occ) ** 0.75
    ao[opaque] = acc / ao_samples
    return sun_vis, ao


def joint_bilateral_filter(
    signals: list[np.ndarray],
    depth: np.ndarray,
    normal: np.ndarray,
    kind: np.ndarray,
    radius: int = 3,
) -> list[np.ndarray]:
    """
    Edge-preserving (H, W) denoise of Monte Carlo visibility buffers, weighted by
    spatial distance, relative depth, normal agreement and matching surface kind.
    """
    h, w = depth.shape
    pad = radius
    d_p = np.pad(depth, pad, mode="edge")
    n_p = np.pad(normal, ((pad, pad), (pad, pad), (0, 0)), mode="edge")
    k_p = np.pad(kind, pad, mode="edge")
    s_p = [np.pad(s, pad, mode="edge") for s in signals]
    sigma_s = 0.5 * radius + 0.5

    acc = [np.zeros((h, w)) for _ in signals]
    wsum = np.zeros((h, w))
    safe_depth = np.maximum(depth, 1e-6)
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            sl = (slice(pad + dy, pad + dy + h), slice(pad + dx, pad + dx + w))
            w_s = math.exp(-(dx * dx + dy * dy) / (2.0 * sigma_s * sigma_s))
            rel = (d_p[sl] - depth) / safe_depth
            w_d = np.exp(-(rel * 60.0) ** 2)
            w_n = np.maximum(dot(n_p[sl], normal), 0.0) ** 16
            wgt = w_s * w_d * w_n * (k_p[sl] == kind)
            wsum += wgt
            for a, s in zip(acc, s_p):
                a += wgt * s[sl]
    wsum = np.maximum(wsum, 1e-8)
    return [a / wsum for a in acc]


# ==============================================================================
# 8. SUN & SKY SHADING
# ==============================================================================


def evaluate_studio_backdrop(rd: np.ndarray) -> np.ndarray:
    """Seamless horizon-to-zenith sky gradient; downward rays see the horizon color."""
    y = np.clip(rd[..., 1], 0.0, 1.0)
    w = smoothstep(0.0, 0.85, y) ** 0.8
    return SKY_HORIZON_RAD * (1.0 - w)[..., None] + SKY_ZENITH_RAD * w[..., None]


def sky_ambient(normal: np.ndarray) -> np.ndarray:
    """
    Cosine-convolved sky + ground-bounce radiance seen by a Lambertian surface with
    the given normal (already divided by PI, so ambient = albedo * sky_ambient * AO).
    """
    l_sun = sun_direction()
    sky_avg = 0.5 * (SKY_ZENITH_RAD + SKY_HORIZON_RAD)
    ground_bounce = GROUND_ALBEDO * (SUN_RADIANCE * max(l_sun[1], 0.0) / PI + sky_avg)
    up_w = 0.5 + 0.5 * normal[..., 1]
    return sky_avg * up_w[..., None] + ground_bounce * (1.0 - up_w)[..., None]


def shade_scene(
    hit: dict[str, np.ndarray],
    rd: np.ndarray,
    sun_vis: np.ndarray,
    ao: np.ndarray,
) -> np.ndarray:
    """Sun (Cook-Torrance), sky ambient, glossy sky reflection and ground fade-out."""
    rad = evaluate_studio_backdrop(rd).copy()
    opaque = np.nonzero(hit["kind"] != KIND_MISS)[0]
    if opaque.size == 0:
        return rad

    ns = hit["normal"][opaque]
    alb = hit["albedo"][opaque]
    rgh = hit["roughness"][opaque]
    f0 = np.repeat(hit["f0"][opaque, None], 3, axis=1)
    v = -rd[opaque]
    ao_o = ao[opaque]

    l_sun = sun_direction()
    diff, spec = cook_torrance_brdf(ns, v, l_sun, alb, rgh, f0)
    direct = (diff + spec) * SUN_RADIANCE[None, :] * sun_vis[opaque, None]

    n_dot_v = np.clip(dot(ns, v), 0.0, 1.0)
    fres = schlick_fresnel(n_dot_v, f0)
    ambient = alb * sky_ambient(ns) * (1.0 - fres) * ao_o[:, None]
    refl = reflect(-v, ns)
    gloss = (1.0 - rgh) ** 2
    env_spec = evaluate_studio_backdrop(refl) * fres * (gloss * ao_o)[:, None]

    surf = direct + ambient + env_spec

    # Fade the infinite ground into the backdrop so the horizon line disappears
    is_ground = hit["kind"][opaque] == KIND_GROUND
    r_xz = np.hypot(hit["pos"][opaque, 0], hit["pos"][opaque, 2])
    fade = np.where(is_ground, smoothstep(GROUND_FADE_START, GROUND_FADE_END, r_xz), 0.0)
    rad[opaque] = surf * (1.0 - fade)[:, None] + rad[opaque] * fade[:, None]
    return rad


# ==============================================================================
# 9. CAMERA OPTICS & ACES TONE MAPPING
# ==============================================================================


def apply_camera_optics_and_aces(
    hdr_radiance: np.ndarray,
    uu: np.ndarray,
    vv: np.ndarray,
    seed: int = 20301,
) -> np.ndarray:
    """Bloom, lens vignetting, ACES filmic curve, sRGB encoding and fine film grain."""
    rad = np.maximum(hdr_radiance, 0.0) * EXPOSURE

    lum = 0.2126 * rad[..., 0] + 0.7152 * rad[..., 1] + 0.0722 * rad[..., 2]
    highlights = rad * smoothstep(1.35, 4.20, lum)[..., None]
    h_sub2 = highlights[::2, ::2, :]
    b_sub = gaussian_blur_rgb(h_sub2, radius=2.2)
    bloom = np.repeat(np.repeat(b_sub, 2, axis=0), 2, axis=1)[: rad.shape[0], : rad.shape[1], :]
    rad = rad + 0.06 * bloom

    u_max_ref = max(float(np.max(np.abs(uu))), 1e-6)
    r2_lens = (uu / u_max_ref * 0.62) ** 2 + (vv / u_max_ref * 0.62) ** 2
    cos_lens = 1.0 / np.sqrt(1.0 + r2_lens)
    vignette = 0.50 + 0.50 * (cos_lens ** 3.4)
    rad *= vignette[..., None]

    # ACES Filmic Tone-Mapping
    a = 2.51
    b = 0.03
    c = 2.43
    d = 0.59
    e = 0.14
    mapped = np.clip((rad * (a * rad + b)) / (rad * (c * rad + d) + e), 0.0, 1.0)
    srgb = np.where(mapped <= 0.0031308, 12.92 * mapped, 1.055 * (mapped ** (1.0 / 2.4)) - 0.055)
    srgb = np.clip(srgb, 0.0, 1.0)

    rng = np.random.default_rng(seed + 4040)
    grain_raw = rng.standard_normal(size=srgb.shape[:2])
    midtone_weight = 4.0 * srgb * (1.0 - srgb)
    return np.clip(srgb + grain_raw[..., None] * midtone_weight * 0.006, 0.0, 1.0)


# ==============================================================================
# 10. MAIN RENDERING ENTRY POINT (EDGE SUPERSAMPLING)
# ==============================================================================


def render_mesh(model_path: str | Path, width: int = 1600, height: int = 1200, seed: int = 20301) -> Image.Image:
    """Load a PLY mesh and render it to an sRGB image."""
    mesh = normalize_mesh_to_studio(load_ply(model_path))
    bvh = TriangleBVH(mesh)
    rng = np.random.default_rng(seed)

    cam_pos = frame_camera_to_mesh(mesh["positions"], float(width) / float(max(height, 1)))
    rd_img, uu, vv = generate_camera_rays(width, height)
    rd = rd_img.reshape(-1, 3)

    hit = trace_scene(bvh, mesh, cam_pos, rd)
    sun_vis, ao = compute_visibility(hit, bvh, rng)

    kind_img = hit["kind"].reshape(height, width)
    depth_img = np.where(np.isfinite(hit["t"]), hit["t"], 1e6).reshape(height, width)
    normal_img = hit["normal"].reshape(height, width, 3)
    sun_vis, ao = joint_bilateral_filter(
        [sun_vis.reshape(height, width), ao.reshape(height, width)], depth_img, normal_img, kind_img
    )
    hdr = shade_scene(hit, rd, sun_vis.reshape(-1), ao.reshape(-1)).reshape(height, width, 3)

    # Selective 4-tap rotated-grid supersampling on silhouettes and creases
    edge = (
        (kind_img != np.roll(kind_img, 1, axis=0))
        | (kind_img != np.roll(kind_img, 1, axis=1))
        | (np.abs(depth_img - np.roll(depth_img, 1, axis=0)) > 0.02 * depth_img)
        | (np.abs(depth_img - np.roll(depth_img, 1, axis=1)) > 0.02 * depth_img)
    )
    edge = edge | np.roll(edge, -1, axis=0) | np.roll(edge, -1, axis=1)
    edge[[0, -1], :] = False
    edge[:, [0, -1]] = False

    if np.any(edge):
        forward, right, up = build_camera_basis()
        du = float(uu[0, 1] - uu[0, 0])
        dv = float(vv[0, 0] - vv[1, 0])
        u_e = uu[edge]
        v_e = vv[edge]
        accum = hdr[edge] * 0.2
        for ox, oy in ((-0.375, 0.125), (0.125, 0.375), (0.375, -0.125), (-0.125, -0.375)):
            d_s = normalize(
                forward[None, :]
                + (u_e + ox * du)[:, None] * right[None, :]
                + (v_e + oy * dv)[:, None] * up[None, :]
            )
            h_s = trace_scene(bvh, mesh, cam_pos, d_s)
            sv_s, ao_s = compute_visibility(h_s, bvh, rng, sun_samples=2, ao_samples=4)
            accum += 0.2 * shade_scene(h_s, d_s, sv_s, ao_s)
        hdr[edge] = accum

    srgb = apply_camera_optics_and_aces(hdr, uu, vv, seed=seed)
    rgb_u8 = np.clip(np.round(srgb * 255.0), 0, 255).astype(np.uint8)
    return Image.fromarray(rgb_u8, mode="RGB")


def main() -> None:
    is_16x9 = ("--16x9" in sys.argv) or (os.environ.get("ASPECT_16X9", "0") == "1")
    pos_args = [a for a in sys.argv[1:] if a != "--16x9"]

    model_path = Path(pos_args[0]) if pos_args else Path(os.environ.get("RENDER_MODEL", DEFAULT_MODEL_PATH))
    width = int(os.environ.get("PAINT_WIDTH", "1920" if is_16x9 else "1600"))
    height = int(os.environ.get("PAINT_HEIGHT", "1080" if is_16x9 else "1200"))
    seed = int(os.environ.get("PAINT_SEED", "20301"))
    default_out = f"renders/{model_path.stem}_{width}x{height}.png"
    out_arg = pos_args[1] if len(pos_args) > 1 else os.environ.get("PAINT_OUTPUT", default_out)

    out_path = Path(out_arg)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    img = render_mesh(model_path, width=width, height=height, seed=seed)
    img.save(out_path, format="PNG", optimize=False)
    print(f"Saved {out_path} ({width}x{height}, seed={seed}, model={model_path})")


if __name__ == "__main__":
    main()
