import * as THREE from 'https://cdn.jsdelivr.net/npm/three@0.180.0/+esm';

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
  // Production-integration note (2026-08-10, not present in the
  // widgets/interactive-cloth prototype): that prototype only ever hosted
  // this canvas inside a bounded panel or a scroll-free full-screen splash
  // (widgets/holocloth-faithful), so 'none' was harmless there. SONYA's
  // real processing screen mounts this canvas full-viewport OVER a page
  // that can be taller than the viewport at some window sizes, and 'none'
  // was found (via manual full-viewport testing) to suppress native
  // wheel/touch scroll-through entirely — not just gesture panning — once
  // the canvas becomes its own GPU compositor layer. 'pan-y' keeps
  // horizontal gestures reserved for the drag (there is no horizontal
  // scroll on this page anyway) while letting vertical wheel/swipe pass
  // through to the page when the user isn't actually grabbing the cloth.
  canvas.style.touchAction = 'pan-y';

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
