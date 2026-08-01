import * as THREE from 'https://cdn.jsdelivr.net/npm/three@0.160.1/build/three.module.js';
/**
 * SONYA — Cinematic 3D loading sphere.
 * Displacement-noise icosahedron core + wireframe shell + ambient particles.
 * Lazy-starts when #page-processing becomes active, pauses otherwise.
 */
(function () {
	const canvas = document.getElementById('sonya-sphere');
	const page = document.getElementById('page-processing');
	if (!canvas || !page) return;

	let scene, camera, renderer, core, wire, particles, mat, clock;
	let raf = 0;
	let running = false;
	let initialized = false;
	let ro = null;

	function supportsWebGL() {
		try {
			const c = document.createElement('canvas');
			return !!(window.WebGLRenderingContext && (c.getContext('webgl') || c.getContext('experimental-webgl')));
		} catch (e) {
			return false;
		}
	}

	function init() {
		if (initialized || typeof THREE === 'undefined' || !supportsWebGL()) return;
		initialized = true;

		scene = new THREE.Scene();
		camera = new THREE.PerspectiveCamera(42, 1, 0.1, 100);
		camera.position.set(0, 0, 3.35);

		renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true, powerPreference: 'low-power' });
		renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
		renderer.setClearColor(0x000000, 0);

		// ─── Shader: simplex-displaced icosahedron with fresnel gold ────────
		const vert = `
			varying vec3 vNormal;
			varying vec3 vPos;
			varying float vDisp;
			uniform float uTime;

			// --- Simplex noise (Ashima) ---
			vec4 permute(vec4 x){ return mod(((x*34.0)+1.0)*x, 289.0); }
			vec4 taylorInvSqrt(vec4 r){ return 1.79284291400159 - 0.85373472095314 * r; }
			float snoise(vec3 v){
				const vec2 C = vec2(1.0/6.0, 1.0/3.0);
				const vec4 D = vec4(0.0, 0.5, 1.0, 2.0);
				vec3 i  = floor(v + dot(v, C.yyy));
				vec3 x0 = v - i + dot(i, C.xxx);
				vec3 g = step(x0.yzx, x0.xyz);
				vec3 l = 1.0 - g;
				vec3 i1 = min(g.xyz, l.zxy);
				vec3 i2 = max(g.xyz, l.zxy);
				vec3 x1 = x0 - i1 + C.xxx;
				vec3 x2 = x0 - i2 + C.yyy;
				vec3 x3 = x0 - D.yyy;
				i = mod(i, 289.0);
				vec4 p = permute(permute(permute(
						i.z + vec4(0.0, i1.z, i2.z, 1.0))
						+ i.y + vec4(0.0, i1.y, i2.y, 1.0))
						+ i.x + vec4(0.0, i1.x, i2.x, 1.0));
				float n_ = 1.0/7.0;
				vec3 ns = n_ * D.wyz - D.xzx;
				vec4 j = p - 49.0 * floor(p * ns.z * ns.z);
				vec4 x_ = floor(j * ns.z);
				vec4 y_ = floor(j - 7.0 * x_);
				vec4 x = x_ * ns.x + ns.yyyy;
				vec4 y = y_ * ns.x + ns.yyyy;
				vec4 h = 1.0 - abs(x) - abs(y);
				vec4 b0 = vec4(x.xy, y.xy);
				vec4 b1 = vec4(x.zw, y.zw);
				vec4 s0 = floor(b0)*2.0 + 1.0;
				vec4 s1 = floor(b1)*2.0 + 1.0;
				vec4 sh = -step(h, vec4(0.0));
				vec4 a0 = b0.xzyw + s0.xzyw*sh.xxyy;
				vec4 a1 = b1.xzyw + s1.xzyw*sh.zzww;
				vec3 p0 = vec3(a0.xy, h.x);
				vec3 p1 = vec3(a0.zw, h.y);
				vec3 p2 = vec3(a1.xy, h.z);
				vec3 p3 = vec3(a1.zw, h.w);
				vec4 norm = taylorInvSqrt(vec4(dot(p0,p0), dot(p1,p1), dot(p2,p2), dot(p3,p3)));
				p0 *= norm.x; p1 *= norm.y; p2 *= norm.z; p3 *= norm.w;
				vec4 m = max(0.6 - vec4(dot(x0,x0), dot(x1,x1), dot(x2,x2), dot(x3,x3)), 0.0);
				m = m * m;
				return 42.0 * dot(m*m, vec4(dot(p0,x0), dot(p1,x1), dot(p2,x2), dot(p3,x3)));
			}

			void main() {
				float n = snoise(position * 1.35 + vec3(0.0, uTime * 0.22, uTime * 0.12));
				float disp = n * 0.11 + 0.04 * sin(uTime * 0.9 + position.y * 2.0);
				vec3 pos = position + normal * disp;

				vDisp = disp;
				vPos = pos;
				vNormal = normalize(normalMatrix * normal);

				gl_Position = projectionMatrix * modelViewMatrix * vec4(pos, 1.0);
			}
		`;

		const frag = `
			precision highp float;
			varying vec3 vNormal;
			varying vec3 vPos;
			varying float vDisp;
			uniform float uTime;

			void main() {
				vec3 viewDir = normalize(cameraPosition - vPos);
				float fres = pow(1.0 - max(dot(vNormal, viewDir), 0.0), 2.6);

				// Warm dune palette
				vec3 cHot   = vec3(0.97, 0.85, 0.58);   // F6E3BE
				vec3 cWarm  = vec3(0.91, 0.72, 0.42);   // E8C58E-ish
				vec3 cDeep  = vec3(0.18, 0.10, 0.05);   // warm near-black
				vec3 cShadow= vec3(0.08, 0.05, 0.03);

				// Core gradient by disp (highlights on ridges)
				vec3 body = mix(cShadow, cDeep, smoothstep(-0.1, 0.05, vDisp));
				body = mix(body, cWarm * 0.5, smoothstep(0.0, 0.12, vDisp));

				// Edge fresnel → hot gold
				vec3 col = mix(body, cHot, fres * 0.85);

				// Subtle inner pulse
				float pulse = 0.5 + 0.5 * sin(uTime * 0.8);
				col += cWarm * 0.06 * pulse;

				// Soft bottom-to-top falloff for dimensional lift
				col *= 0.85 + 0.3 * (vPos.y * 0.5 + 0.5);

				gl_FragColor = vec4(col, 1.0);
			}
		`;

		mat = new THREE.ShaderMaterial({
			vertexShader: vert,
			fragmentShader: frag,
			uniforms: { uTime: { value: 0 } }
		});

		const coreGeom = new THREE.IcosahedronGeometry(1.0, 40);
		core = new THREE.Mesh(coreGeom, mat);
		scene.add(core);

		// Wireframe shell
		const wireGeom = new THREE.IcosahedronGeometry(1.32, 2);
		const wireMat = new THREE.MeshBasicMaterial({
			color: 0xE8C58E,
			wireframe: true,
			transparent: true,
			opacity: 0.14
		});
		wire = new THREE.Mesh(wireGeom, wireMat);
		scene.add(wire);

		// Ambient particle field
		const N = 520;
		const pGeom = new THREE.BufferGeometry();
		const positions = new Float32Array(N * 3);
		for (let i = 0; i < N; i++) {
			const r = 1.7 + Math.random() * 1.9;
			const theta = Math.random() * Math.PI * 2;
			const phi = Math.acos(2 * Math.random() - 1);
			positions[i * 3 + 0] = r * Math.sin(phi) * Math.cos(theta);
			positions[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta);
			positions[i * 3 + 2] = r * Math.cos(phi);
		}
		pGeom.setAttribute('position', new THREE.BufferAttribute(positions, 3));
		const pMat = new THREE.PointsMaterial({
			color: 0xE8C58E,
			size: 0.018,
			transparent: true,
			opacity: 0.55,
			blending: THREE.AdditiveBlending,
			depthWrite: false,
			sizeAttenuation: true
		});
		particles = new THREE.Points(pGeom, pMat);
		scene.add(particles);

		clock = new THREE.Clock();
		resize();

		ro = new ResizeObserver(resize);
		ro.observe(canvas);
	}

	function resize() {
		if (!renderer || !camera || !canvas) return;
		const rect = canvas.getBoundingClientRect();
		const w = Math.max(1, Math.round(rect.width));
		const h = Math.max(1, Math.round(rect.height));
		renderer.setSize(w, h, false);
		camera.aspect = w / h;
		camera.updateProjectionMatrix();
	}

	function tick() {
		if (!running) return;
		const t = clock.getElapsedTime();
		if (mat) mat.uniforms.uTime.value = t;
		if (core) {
			core.rotation.y = t * 0.14;
			core.rotation.x = Math.sin(t * 0.18) * 0.18;
		}
		if (wire) {
			wire.rotation.y = -t * 0.09;
			wire.rotation.z = t * 0.04;
		}
		if (particles) {
			particles.rotation.y = t * 0.02;
			particles.rotation.x = Math.cos(t * 0.08) * 0.06;
		}
		renderer.render(scene, camera);
		raf = requestAnimationFrame(tick);
	}

	function start() {
		init();
		if (!renderer || running) return;
		running = true;
		clock.getDelta(); // reset
		cancelAnimationFrame(raf);
		tick();
	}

	function stop() {
		running = false;
		cancelAnimationFrame(raf);
	}

	function isSphereVisible() {
		const wrap = canvas.closest('.sonya-sphere-wrap');
		if (!wrap) return true;
		// The v2 UI hides this wrap and shows title/status/progress-bar instead —
		// don't keep rendering an invisible WebGL scene every frame.
		return getComputedStyle(wrap).display !== 'none';
	}

	function sync() {
		if (page.classList.contains('active') && isSphereVisible()) start();
		else stop();
	}

	// Wait for THREE to be available (CDN may load slightly after DOM)
	function whenReady(cb) {
		if (typeof THREE !== 'undefined') { cb(); return; }
		let attempts = 0;
		const int = setInterval(() => {
			attempts++;
			if (typeof THREE !== 'undefined') { clearInterval(int); cb(); }
			else if (attempts > 80) { clearInterval(int); } // ~8s timeout
		}, 100);
	}

	// Observe processing-page activation
	const obs = new MutationObserver(sync);
	obs.observe(page, { attributes: true, attributeFilter: ['class'] });

	// Pause when tab/window backgrounded
	document.addEventListener('visibilitychange', () => {
		if (document.hidden) stop();
		else sync();
	});

	whenReady(sync);
})();
