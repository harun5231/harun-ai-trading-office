// Batch rendered surfaces without changing the articulated source skeletons.
// Original meshes retain their matrix updates and externally requested visibility.
// This module owns only instance buffers and cloned materials, never rig geometry.

const SHADING_FIELDS = [
  'type', 'side', 'shadowSide', 'opacity', 'transparent', 'alphaTest', 'alphaHash',
  'depthTest', 'depthWrite', 'depthFunc', 'colorWrite', 'blending', 'blendSrc',
  'blendDst', 'blendEquation', 'blendSrcAlpha', 'blendDstAlpha', 'blendEquationAlpha',
  'blendAlpha', 'alphaToCoverage',
  'premultipliedAlpha', 'polygonOffset', 'polygonOffsetFactor', 'polygonOffsetUnits',
  'dithering', 'toneMapped', 'vertexColors', 'flatShading', 'wireframe',
  'wireframeLinewidth', 'precision', 'fog', 'roughness', 'metalness',
  'emissiveIntensity', 'envMapIntensity', 'combine', 'reflectivity', 'refractionRatio',
  'stencilWrite', 'stencilWriteMask', 'stencilFunc', 'stencilRef', 'stencilFuncMask',
  'stencilFail', 'stencilZFail', 'stencilZPass', 'forceSinglePass',
];

function eligible(THREE, mesh) {
  const mat = mesh.material;
  const visibility = Object.getOwnPropertyDescriptor(mesh, 'visible');
  return mesh.isMesh && !mesh.isInstancedMesh && !mesh.isSkinnedMesh
    && mesh.geometry && !Array.isArray(mat)
    && (mat?.type === 'MeshStandardMaterial' || mat?.type === 'MeshBasicMaterial')
    && mat.color && !mat.transparent && mat.opacity === 1
    && !Object.values(mat).some(value => value?.isTexture)
    && !mat.clippingPlanes?.length
    && !mesh.customDepthMaterial && !mesh.customDistanceMaterial
    && mat.onBeforeCompile === THREE.Material.prototype.onBeforeCompile
    && !Object.keys(mesh.geometry.morphAttributes || {}).length
    && (!visibility || (visibility.configurable && 'value' in visibility));
}

function keyFor(mesh) {
  const mat = mesh.material;
  return JSON.stringify([
    mesh.geometry.uuid,
    ...SHADING_FIELDS.map(field => mat[field] ?? null),
    mat.emissive?.toArray() ?? null,
    mat.blendColor?.toArray() ?? null,
    mesh.castShadow, mesh.receiveShadow, mesh.renderOrder, mesh.layers.mask,
  ]);
}

export function createAvatarBatch(THREE, { scene, actors = [] } = {}) {
  if (!scene?.isObject3D || !THREE?.InstancedMesh) throw new TypeError('Avatar batch requires Three.js and a scene');
  let disposed = false;
  const sources = [], candidates = new Map(), batches = [];
  const renderVisibility = new WeakMap();
  const seen = new Set();
  for (const actor of actors) {
    const root = actor?.avatar?.root || actor?.root || (actor?.isObject3D ? actor : null);
    if (!root) continue;
    root.traverse(mesh => {
      if (!mesh.isMesh || seen.has(mesh)) return;
      seen.add(mesh);
      sources.push(mesh);
      if (!eligible(THREE, mesh)) return;
      const key = keyFor(mesh);
      if (!candidates.has(key)) candidates.set(key, []);
      candidates.get(key).push(mesh);
    });
  }

  for (const members of candidates.values()) {
    // Unique curves, lapels, and map-backed badges remain ordinary meshes.
    if (members.length < 2) continue;
    const source = members[0];
    const mat = source.material.clone();
    mat.color.setRGB(1, 1, 1);
    const instances = new THREE.InstancedMesh(source.geometry, mat, members.length);
    instances.name = `avatar-surfaces-${batches.length}`;
    instances.castShadow = source.castShadow;
    instances.receiveShadow = source.receiveShadow;
    instances.renderOrder = source.renderOrder;
    instances.layers.mask = source.layers.mask;
    instances.frustumCulled = false;
    instances.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    instances.userData.visualAvatarBatch = true;
    const records = members.map((mesh, index) => {
      const descriptor = Object.getOwnPropertyDescriptor(mesh, 'visible');
      const record = { mesh, index, descriptor, intendedVisible: mesh.visible };
      renderVisibility.set(mesh, record);
      instances.setColorAt(index, mesh.material.color);
      // Renderer sees false; callers can still set mesh.visible and change the
      // instance visibility. Restoring the descriptor preserves their last value.
      Object.defineProperty(mesh, 'visible', {
        configurable: true, enumerable: descriptor?.enumerable ?? true,
        get: () => disposed ? record.intendedVisible : false,
        set: value => { record.intendedVisible = value; },
      });
      return record;
    });
    instances.instanceColor.needsUpdate = true;
    scene.add(instances);
    batches.push({ instances, mat, records });
  }

  const inverse = new THREE.Matrix4(), matrix = new THREE.Matrix4();
  const hidden = new THREE.Matrix4().makeScale(0, 0, 0);
  function visible(record) {
    if (record.intendedVisible === false) return false;
    let inScene = false;
    for (let ancestor = record.mesh.parent; ancestor; ancestor = ancestor.parent) {
      if (ancestor === scene) inScene = true;
      const intent = renderVisibility.get(ancestor);
      if ((intent ? intent.intendedVisible : ancestor.visible) === false) return false;
    }
    return inScene;
  }
  function update() {
    if (disposed) return;
    // Call after scene.updateMatrixWorld(true); source meshes update even though
    // their render visibility is false. Respect transformed scene parents too.
    for (const batch of batches) {
      inverse.copy(batch.instances.matrixWorld).invert();
      for (const record of batch.records) {
        if (visible(record)) {
          matrix.multiplyMatrices(inverse, record.mesh.matrixWorld);
          batch.instances.setMatrixAt(record.index, matrix);
        } else batch.instances.setMatrixAt(record.index, hidden);
      }
      batch.instances.instanceMatrix.needsUpdate = true;
    }
  }
  function diagnostics() {
    const batchedMeshes = batches.reduce((count, batch) => count + batch.records.length, 0);
    const unbatchedMeshes = sources.length - batchedMeshes;
    return { disposed, sourceMeshes: sources.length, batchedMeshes,
      instancedGroups: batches.length, unbatchedMeshes,
      estimatedAvatarDrawCalls: batches.length + unbatchedMeshes,
      estimatedSavedDrawCalls: batchedMeshes - batches.length };
  }
  function dispose() {
    if (disposed) return;
    disposed = true;
    for (const batch of batches) {
      for (const record of batch.records) {
        const descriptor = record.descriptor
          ? { ...record.descriptor, value: record.intendedVisible }
          : { configurable: true, enumerable: true, writable: true, value: record.intendedVisible };
        Object.defineProperty(record.mesh, 'visible', descriptor);
      }
      batch.instances.removeFromParent();
      batch.instances.dispose();
      batch.mat.dispose();
    }
  }
  scene.updateMatrixWorld(true);
  update();
  return { update, dispose, diagnostics };
}
