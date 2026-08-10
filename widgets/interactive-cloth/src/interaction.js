import * as THREE from 'three';

/**
 * Pointer/touch grab-drag-release interaction for the cloth.
 *
 * The pointerdown/move/up lifecycle, pointer-capture handling, and the
 * camera-facing drag-plane projection are adapted from holocloth's
 * `src/scene.ts` (`HoloApp`'s `onPointerDown/Move/Up`) — see
 * third_party/holocloth/NOTICE.md. OrbitControls and the decal/edit-mode
 * branch were dropped: SONYA's cloth is a single grab-and-throw surface,
 * not a camera-orbiting composition tool.
 *
 * @param {{
 *   renderer: THREE.WebGLRenderer,
 *   camera: THREE.PerspectiveCamera,
 *   clothMesh: THREE.Mesh,
 *   sim: import('./clothPhysics.js').ClothSim,
 *   getGrabRadius: () => number,
 * }} deps
 * @returns {{ dispose: () => void }}
 */
export function attachInteraction({ renderer, camera, clothMesh, sim, getGrabRadius }) {
  const canvas = renderer.domElement;
  const raycaster = new THREE.Raycaster();
  const pointerNdc = new THREE.Vector2();
  const dragPlane = new THREE.Plane();
  let grabbing = false;
  let grabPointerId = null;

  function updatePointer(e) {
    const rect = canvas.getBoundingClientRect();
    pointerNdc.set(
      ((e.clientX - rect.left) / rect.width) * 2 - 1,
      -((e.clientY - rect.top) / rect.height) * 2 + 1,
    );
  }

  function raycastCloth() {
    raycaster.setFromCamera(pointerNdc, camera);
    const hits = raycaster.intersectObject(clothMesh, false);
    return hits.length > 0 ? hits[0] : null;
  }

  function onPointerDown(e) {
    if (e.button !== 0 || grabbing) return;
    updatePointer(e);
    const hit = raycastCloth();
    if (!hit) return;
    if (!sim.startGrab(hit.point, getGrabRadius())) return;
    grabbing = true;
    grabPointerId = e.pointerId;
    const normal = new THREE.Vector3();
    camera.getWorldDirection(normal);
    dragPlane.setFromNormalAndCoplanarPoint(normal, hit.point);
    canvas.setPointerCapture(e.pointerId);
    canvas.style.cursor = 'grabbing';
  }

  function onPointerMove(e) {
    if (!grabbing || e.pointerId !== grabPointerId) {
      if (!grabbing) {
        updatePointer(e);
        canvas.style.cursor = raycastCloth() ? 'grab' : 'default';
      }
      return;
    }
    updatePointer(e);
    raycaster.setFromCamera(pointerNdc, camera);
    const target = new THREE.Vector3();
    if (raycaster.ray.intersectPlane(dragPlane, target)) {
      sim.moveGrab(target);
    }
  }

  function endGrab(e) {
    if (!grabbing || (e && e.pointerId !== grabPointerId)) return;
    grabbing = false;
    grabPointerId = null;
    sim.endGrab();
    canvas.style.cursor = 'grab';
    if (e && canvas.hasPointerCapture(e.pointerId)) canvas.releasePointerCapture(e.pointerId);
  }

  canvas.addEventListener('pointerdown', onPointerDown);
  canvas.addEventListener('pointermove', onPointerMove);
  canvas.addEventListener('pointerup', endGrab);
  canvas.addEventListener('pointercancel', endGrab);
  // Interaction is scoped to the canvas element only — pointer events don't
  // bubble into page scroll handling, so this never blocks scrolling
  // elsewhere on the page.
  canvas.style.touchAction = 'none';

  return {
    dispose() {
      canvas.removeEventListener('pointerdown', onPointerDown);
      canvas.removeEventListener('pointermove', onPointerMove);
      canvas.removeEventListener('pointerup', endGrab);
      canvas.removeEventListener('pointercancel', endGrab);
      sim.endGrab();
    },
  };
}
