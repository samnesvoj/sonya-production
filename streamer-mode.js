/* SONYA — Режим стримера v2
   Источник → анализ + выбор тем → обработка и проверка клипов.

   Источник/анализ/темы/клипы теперь backed by the real streamer batch
   flow (POST/GET /api/streamer/batches..., see the REAL BATCH FLOW
   section below and scripts/streamer_routes.py) — submitSource() /
   pollBatchUntilSelectable() / confirmSelection() / pollBatchUntilDone() /
   applyBatchClips() / rehydrateFromUrl() replaced the old mock timers,
   reusing every render*Step()/clipCardHTML()/topicCardHTML() function
   unchanged. Subtitle quick-editor, promo assets, and single-clip retry
   remain explicit mock/unwired — see "NOT DONE YET" at the end of the
   file and each function's own comment. */
(() => {
	'use strict';

	const $ = (sel, root = document) => root.querySelector(sel);
	const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

	function escapeHtml(str) {
		return String(str)
			.replace(/&/g, '&amp;')
			.replace(/</g, '&lt;')
			.replace(/>/g, '&gt;');
	}
	function escapeAttr(str) {
		return String(str).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
	}

	/* ============================================================
	   TOAST — small self-contained helper, visually compatible with
	   the existing .sonya-toast rules already shipped in styles.css
	   (see auth.js showToast() for the production twin of this).
	   No external libraries, no new CSS added here on purpose.
	   ============================================================ */
	function showToast(message, type = 'info') {
		let toast = document.getElementById('sm-toast');
		if (!toast) {
			toast = document.createElement('div');
			toast.id = 'sm-toast';
			document.body.appendChild(toast);
		}
		const icons = { success: '✓', error: '⚠', info: '' };
		const ic = icons[type] || '';
		toast.innerHTML = ic
			? `<span class="sonya-toast-ic" aria-hidden="true">${ic}</span><span>${escapeHtml(message)}</span>`
			: escapeHtml(message);
		toast.className = `sonya-toast sonya-toast--${type} is-visible`;
		clearTimeout(toast._timer);
		toast._timer = setTimeout(() => toast.classList.remove('is-visible'), 4200);
	}

	/* ============================================================
	   SOURCE — URL platform detection & file validation.
	   Platform detection is a minimal port of app.js::detectPlatform()
	   (mirrors scripts/url_ingest.py::detect_platform() server-side) —
	   NOT copied in full, just the client-side pre-filter this page
	   needs. Backend remains the source of truth once wired up.
	   ============================================================ */
	const DIRECT_VIDEO_EXT_RE = /\.(mp4|mov|avi|mkv|webm|mpeg|mpg|3gp|m3u8)(\?.*)?$/i;

	function detectPlatform(url) {
		if (!url) return 'unknown';
		let parsed;
		try {
			parsed = new URL(url.trim());
		} catch (_) {
			return 'unknown';
		}
		if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return 'unknown';
		const host = parsed.hostname.toLowerCase();
		if (host === 'youtube.com' || host === 'www.youtube.com' || host === 'm.youtube.com' ||
			host === 'music.youtube.com' || host === 'youtu.be' || host.endsWith('.youtube.com')) {
			return 'youtube';
		}
		if (host === 'vk.com' || host === 'www.vk.com' || host === 'm.vk.com' ||
			host === 'vkvideo.ru' || host === 'www.vkvideo.ru' ||
			host.endsWith('.vk.com') || host.endsWith('.vkvideo.ru')) {
			return 'vk';
		}
		if (host === 'twitch.tv' || host === 'www.twitch.tv' || host === 'm.twitch.tv' ||
			host === 'clips.twitch.tv' || host.endsWith('.twitch.tv')) {
			return 'twitch';
		}
		if (DIRECT_VIDEO_EXT_RE.test(parsed.pathname)) return 'direct';
		return 'unknown';
	}

	const PLATFORM_ICON_CLASS = {
		youtube: 'fa-brands fa-youtube',
		vk: 'fa-brands fa-vk',
		twitch: 'fa-brands fa-twitch',
		direct: 'fa-solid fa-file-video',
		unknown: 'fa-solid fa-link',
	};

	// Placeholder limit — the real ceiling belongs to the upload backend
	// once it exists; this just keeps the mock flow honest about VOD
	// files being large.
	const MAX_FILE_SIZE_BYTES = 8 * 1024 * 1024 * 1024; // 8 GB
	const ACCEPTED_FILE_EXT_RE = /\.(mp4|mov|mkv|webm|avi|m4v)$/i;

	function formatFileSize(bytes) {
		if (!Number.isFinite(bytes)) return '';
		const units = ['Б', 'КБ', 'МБ', 'ГБ'];
		let val = bytes, i = 0;
		while (val >= 1024 && i < units.length - 1) { val /= 1024; i++; }
		return `${val < 10 && i > 0 ? val.toFixed(1) : Math.round(val)} ${units[i]}`;
	}

	function validateFile(file) {
		const isVideoMime = file.type && file.type.startsWith('video/');
		const isVideoExt = ACCEPTED_FILE_EXT_RE.test(file.name || '');
		if (!isVideoMime && !isVideoExt) {
			return { ok: false, reason: 'Неподдерживаемый формат файла. Используйте MP4, MOV, MKV, WEBM или AVI.' };
		}
		if (file.size > MAX_FILE_SIZE_BYTES) {
			return { ok: false, reason: `Файл слишком большой (${formatFileSize(file.size)}). Максимум — ${formatFileSize(MAX_FILE_SIZE_BYTES)}.` };
		}
		return { ok: true };
	}

	/* ============================================================
	   MOCK DATA — stands in for the not-yet-built topic-detection
	   and clip-generation APIs. Shaped the way the real API response
	   is expected to look, so swapping this out later shouldn't
	   require changing renderTopicsStep()/renderClipsStep().
	   ============================================================ */
	// Loading-state copy for each real streamer_batches.status value while
	// a batch is still being ingested/analyzed — see pollBatchUntilSelectable()
	// and rehydrateFromUrl() in the REAL BATCH FLOW section below.
	const ANALYSIS_STAGE_LABELS = {
		queued: 'Ставим в очередь',
		ingesting: 'Получаем видео',
		analyzing: 'Анализируем стрим',
	};

	// Built-in subtitle presets. "Свой пресет" (see openPresetDrawer's
	// subtitles tab) just pushes a clone of the currently-selected one
	// onto this same array — no separate custom-preset storage.
	const MOCK_SUBTITLE_PRESETS = [
		{ id: 'sub-default', name: 'SONYA Default', isDefault: true, size: 'm', position: 'bottom', maxLines: 2, activeWordHighlight: true },
		{ id: 'sub-bold', name: 'Крупный акцент', isDefault: false, size: 'l', position: 'bottom', maxLines: 1, activeWordHighlight: true },
		{ id: 'sub-minimal', name: 'Минимал', isDefault: false, size: 's', position: 'top', maxLines: 2, activeWordHighlight: false },
	];

	// Starts empty — the promo library is something the account builds up
	// by adding demo assets via the dropzone in the preset drawer, not
	// pre-seeded content.
	const MOCK_PROMO_ASSETS = [];

	const PROMO_RULES = [
		{ id: 'intro', label: 'В начале' },
		{ id: 'between', label: 'Между фрагментами' },
		{ id: 'outro', label: 'В конце' },
		{ id: 'overlay', label: 'Поверх видео' },
		{ id: 'suggest', label: 'Только предложить' },
	];

	// Account-level defaults for a fresh Streamer Preset — golden path
	// needs zero clicks here; "Настроить" is only for people who want to
	// change something.
	const MOCK_STREAMER_PRESET = {
		activeSubtitlePresetId: 'sub-default',
		autoReviewMode: 'manual', // 'manual' | 'recommend' | 'auto'
		telegramNotifyEnabled: false,
		// telegramLinked/telegramLinkedAt are seeded false/null here and
		// overwritten by the real GET /api/telegram/status on load — see
		// fetchTelegramStatus(). Everything else in accountPreset is still
		// pure mock; this is the one field backed by a real endpoint.
		telegramLinked: false,
		telegramLinkedAt: null,
	};

	function timecodeToSeconds(tc) {
		const parts = String(tc).split(':').map(Number);
		return parts.reduce((acc, v) => acc * 60 + v, 0);
	}
	function formatDuration(seconds) {
		const m = Math.floor(seconds / 60);
		const s = Math.round(seconds % 60);
		return `${m}:${String(s).padStart(2, '0')}`;
	}

	/* ============================================================
	   STATE — split by concern so each render*Step() only reads its
	   own slice. `source`/`analysis`/`topics`/`clips` map 1:1 to the
	   3 rail steps (source == step "upload").
	   ============================================================ */
	const state = {
		source: {
			mode: 'url',            // 'url' | 'file'
			url: '',
			platform: 'unknown',
			file: null,
			fileStatus: 'idle',     // idle | uploading | ready | error
			uploadProgress: 0,
			error: null,
		},
		analysis: {
			status: 'idle',         // idle | running | error | done
			stageLabel: '',
			progress: 0,
			error: null,
		},
		topics: {
			items: [],
		},
		clips: {
			items: [],
			autoMode: false,
		},
		// Real streamer_batches.id once POST /api/streamer/batches (or
		// rehydration from ?batch=) succeeds — see the REAL BATCH FLOW
		// section below. null until then; also doubles as the "supersede a
		// stray poll from a previous batch" guard (each poll loop captures
		// it at start and bails the moment it no longer matches).
		batchId: null,
		// Account-level "Streamer Preset" — lives above any single batch,
		// edited via the drawer (see section E). Seeded from mock data now;
		// a real account would load/save this server-side.
		accountPreset: {
			subtitlePresets: MOCK_SUBTITLE_PRESETS.map(p => ({ ...p })),
			promoAssets: MOCK_PROMO_ASSETS.map(a => ({ ...a })),
			...MOCK_STREAMER_PRESET,
		},
	};
	let presetDrawerActiveTab = 'subtitles';
	let subtitleEditorClipId = null; // which clip's drawer is currently open

	/* ── Cloth engine ──
	   Reuses SONYA's existing "cloth with logo" loading asset — the same
	   Three.js widget + sonya-cloth-dark.jpg texture the site's real
	   processing-screen build (widgets/interactive-cloth) already uses —
	   instead of a spinner. Its SonyaCloth.mount() is a page-wide singleton
	   (see SonyaCloth.js), so one offscreen instance is rendered once and
	   blitted into every "in progress" canvas each frame. Unchanged from
	   v1 other than being attached only while something is genuinely
	   loading/processing (see runAnalysis() and wireClipCard()). */
	const ClothEngine = (() => {
		const CHUNK_URL = '/widgets/interactive-cloth/dist/chunks/SonyaCloth-Cv5-vxXY.js';
		const CONFIG = {
			texture: { url: '/widgets/interactive-cloth/dist/sonya-cloth-dark.jpg', fit: 'cover', scale: 1, offsetX: 0, offsetY: 0, rotation: 0 },
			material: {
				baseColor: '#ded6fb',
				secondaryColor: '#efe9ff',
				holoIntensity: 0,
				iridescence: 0,
				roughness: 0.16,
				metalness: 0.85,
				opacity: 1,
				glow: 0,
				lightIntensity: 0.9,
				clearcoat: 0.85,
				clearcoatRoughness: 0.06,
				sheen: 0,
			},
			lighting: {
				rimAColor: '#8A6BFF',
				rimAIntensity: 2.0,
				rimBColor: '#2FE6D6',
				rimBIntensity: 1.1,
				environmentIntensity: 1.3,
				exposure: 0.6,
			},
			cloth: { width: 5.2, height: 4.4, pins: [], gravity: 0, settleGravity: 0.9, wind: 0.35, damping: 0.22, maxDisplacement: 3.2 },
			scene: { transparentBackground: true, scale: 1.7, cameraPosition: [0, 0, 7.4] },
		};

		let SonyaCloth = null;
		let masterCanvas = null;
		let fallbackUrl = null;
		let started = false;

		async function ensureStarted() {
			if (started) return;
			started = true;
			try {
				const mod = await import(CHUNK_URL);
				SonyaCloth = mod.S;
				const host = document.getElementById('sm-cloth-master');
				SonyaCloth.mount(host, { config: CONFIG });
				masterCanvas = host.querySelector('canvas');
				if (!masterCanvas) {
					fallbackUrl = CONFIG.texture.url;
				}
			} catch (err) {
				console.warn('[streamer-mode] cloth asset failed to load, falling back to static texture', err);
				fallbackUrl = CONFIG.texture.url;
			}
			if (fallbackUrl) $$('canvas.sm-clip-cloth').forEach(applyFallback);
			loop();
		}

		function sizeCanvas(canvas) {
			const dpr = Math.min(window.devicePixelRatio || 1, 2);
			const w = Math.max(1, Math.round(canvas.clientWidth * dpr));
			const h = Math.max(1, Math.round(canvas.clientHeight * dpr));
			if (canvas.width !== w || canvas.height !== h) {
				canvas.width = w;
				canvas.height = h;
			}
		}

		function loop() {
			if (masterCanvas) {
				$$('canvas.sm-clip-cloth:not(.sm-clip-cloth-fallback)').forEach(canvas => {
					sizeCanvas(canvas);
					const ctx = canvas.getContext('2d');
					ctx.drawImage(masterCanvas, 0, 0, canvas.width, canvas.height);
				});
			}
			requestAnimationFrame(loop);
		}

		function applyFallback(canvas) {
			canvas.classList.add('sm-clip-cloth-fallback');
			canvas.style.backgroundImage = `url(${fallbackUrl})`;
		}

		function attach(canvas) {
			ensureStarted();
			if (fallbackUrl) applyFallback(canvas);
		}

		return { attach };
	})();

	/* ============================================================
	   RAIL / STEP NAVIGATION
	   ============================================================ */
	const rail = $('#sm-rail');
	const steps = $$('.sm-step');
	const STEP_ORDER = ['upload', 'topics', 'clips'];

	function goToStep(name) {
		steps.forEach(s => s.classList.toggle('is-active', s.id === 'sm-step-' + name));
		const idx = STEP_ORDER.indexOf(name);
		$$('.sm-rail-step', rail).forEach(li => {
			const liIdx = STEP_ORDER.indexOf(li.dataset.step);
			li.classList.toggle('is-active', liIdx === idx);
			li.classList.toggle('is-done', liIdx < idx);
			const num = $('.sm-rail-num', li);
			num.innerHTML = liIdx < idx ? '<i class="fa-solid fa-check"></i>' : String(liIdx + 1);
		});
		window.scrollTo({ top: 0, behavior: 'smooth' });
	}

	/* ============================================================
	   A. renderSourceStep() — step 1
	   ============================================================ */
	const tabs = $$('.sm-tab');
	const uploadModes = $$('.sm-upload-mode');
	const urlInput = $('#sm-video-url');
	const urlRow = $('#sm-url-row');
	const urlIcon = $('#sm-url-icon');
	const urlMsg = $('#sm-url-msg');
	const dropzone = $('#sm-dropzone');
	const fileInput = $('#sm-file-input');
	const fileChip = $('#sm-file-chip');
	const fileNameEl = $('#sm-file-name');
	const fileSizeEl = $('#sm-file-size');
	const fileProgressWrap = $('#sm-file-progress');
	const fileProgressBar = $('#sm-file-progress-bar');
	const fileRemoveBtn = $('#sm-file-remove');
	const fileMsg = $('#sm-file-msg');
	const btn1 = $('#sm-btn-1');

	function setFieldMsg(el, text, kind) {
		if (!text) { el.hidden = true; el.textContent = ''; el.className = 'sm-field-msg'; return; }
		el.hidden = false;
		el.className = 'sm-field-msg' + (kind ? ' is-' + kind : '');
		el.innerHTML = `<i class="fa-solid ${kind === 'error' ? 'fa-circle-exclamation' : 'fa-circle-info'}"></i><span>${escapeHtml(text)}</span>`;
	}

	function renderSourceStep() {
		tabs.forEach(t => {
			const active = t.dataset.smMode === state.source.mode;
			t.classList.toggle('is-active', active);
			t.setAttribute('aria-selected', String(active));
		});
		uploadModes.forEach(m => m.classList.toggle('is-active', m.id === 'sm-mode-' + state.source.mode));

		// URL sub-state
		urlRow.classList.toggle('is-error', state.source.mode === 'url' && !!state.source.error);
		urlRow.classList.toggle('is-valid', state.source.mode === 'url' && !state.source.error && state.source.platform !== 'unknown');
		urlIcon.className = 'fa-solid ' + 'fa-link';
		if (state.source.platform !== 'unknown' && !state.source.error) {
			urlIcon.className = PLATFORM_ICON_CLASS[state.source.platform];
		}
		if (state.source.mode === 'url') {
			setFieldMsg(urlMsg, state.source.error, state.source.error ? 'error' : null);
		}

		// File sub-state
		fileChip.classList.toggle('is-visible', !!state.source.file);
		dropzone.style.display = state.source.file ? 'none' : '';
		if (state.source.file) {
			fileNameEl.textContent = state.source.file.name;
			fileSizeEl.textContent = formatFileSize(state.source.file.size);
			const uploading = state.source.fileStatus === 'uploading';
			fileProgressWrap.hidden = !uploading;
			fileProgressBar.style.width = uploading ? state.source.uploadProgress + '%' : '0%';
		}
		if (state.source.mode === 'file') {
			setFieldMsg(fileMsg, state.source.fileStatus === 'error' ? state.source.error : null, 'error');
		}

		const ready = state.source.mode === 'url'
			? (state.source.url.trim().length > 0 && state.source.platform !== 'unknown')
			: (state.source.fileStatus === 'ready');
		btn1.disabled = !ready;
	}

	tabs.forEach(tab => {
		tab.addEventListener('click', () => {
			state.source.mode = tab.dataset.smMode;
			renderSourceStep();
		});
	});

	urlInput.addEventListener('input', () => {
		const val = urlInput.value;
		state.source.url = val;
		if (!val.trim()) {
			state.source.platform = 'unknown';
			state.source.error = null;
		} else {
			const platform = detectPlatform(val);
			state.source.platform = platform;
			state.source.error = platform === 'unknown'
				? 'Ссылка не распознана. Поддерживаются Twitch, YouTube VOD, VK или прямая ссылка на видеофайл.'
				: null;
		}
		renderSourceStep();
	});

	function simulateFileUpload() {
		state.source.fileStatus = 'uploading';
		state.source.uploadProgress = 0;
		renderSourceStep();
		const step = () => {
			state.source.uploadProgress = Math.min(100, state.source.uploadProgress + 8 + Math.random() * 10);
			if (state.source.uploadProgress >= 100) {
				state.source.fileStatus = 'ready';
				renderSourceStep();
				return;
			}
			renderSourceStep();
			setTimeout(step, 120);
		};
		setTimeout(step, 120);
	}

	function handleFile(file) {
		if (!file) return;
		const result = validateFile(file);
		if (!result.ok) {
			state.source.file = null;
			state.source.fileStatus = 'error';
			state.source.error = result.reason;
			fileInput.value = '';
			showToast(result.reason, 'error');
			renderSourceStep();
			return;
		}
		state.source.file = file;
		state.source.error = null;
		renderSourceStep();
		simulateFileUpload();
	}
	function removeFile() {
		state.source.file = null;
		state.source.fileStatus = 'idle';
		state.source.uploadProgress = 0;
		state.source.error = null;
		fileInput.value = '';
		renderSourceStep();
	}
	fileInput.addEventListener('change', e => handleFile(e.target.files[0]));
	fileRemoveBtn.addEventListener('click', removeFile);
	dropzone.addEventListener('dragover', e => { e.preventDefault(); dropzone.classList.add('is-dragover'); });
	dropzone.addEventListener('dragleave', () => dropzone.classList.remove('is-dragover'));
	dropzone.addEventListener('drop', e => {
		e.preventDefault();
		dropzone.classList.remove('is-dragover');
		handleFile(e.dataTransfer.files[0]);
	});

	btn1.addEventListener('click', () => { submitSource(); });

	/* ============================================================
	   B. renderAnalysisState() — analysis/loading inside step "topics"
	   ============================================================ */
	const loadingEl = $('#sm-loading');
	const loadingStageEl = $('#sm-loading-stage');
	const loadingProgressBar = $('#sm-loading-progress-bar');
	const analysisErrorEl = $('#sm-analysis-error');
	const analysisErrorMsg = $('#sm-analysis-error-msg');
	const analysisCancelBtn = $('#sm-analysis-cancel');
	const analysisRetryBtn = $('#sm-analysis-retry');
	const analysisBackBtn = $('#sm-analysis-back');
	const topicsBody = $('#sm-topics-body');

	function renderAnalysisState() {
		const st = state.analysis.status;
		loadingEl.classList.toggle('is-hidden', st !== 'running');
		analysisErrorEl.classList.toggle('is-hidden', st !== 'error');
		topicsBody.classList.toggle('is-hidden', st !== 'done');
		if (st === 'running') {
			loadingStageEl.textContent = state.analysis.stageLabel || ANALYSIS_STAGE_LABELS.queued;
			loadingProgressBar.style.width = state.analysis.progress + '%';
		}
		if (st === 'error') {
			analysisErrorMsg.textContent = state.analysis.error || 'Попробуйте ещё раз или вернитесь и проверьте источник.';
		}
	}

	analysisCancelBtn.addEventListener('click', () => {
		stopPolling();
		state.batchId = null;
		state.analysis.status = 'idle';
		state.analysis.progress = 0;
		state.analysis.stageLabel = '';
		history.pushState({}, '', window.location.pathname);
		goToStep('upload');
	});
	// "Попробовать ещё раз" starts a brand-new batch from the same source
	// fields already in state.source — there is no endpoint to retry a
	// specific failed batch's analysis in place (see the REAL BATCH FLOW
	// section below), so this is the honest equivalent of the user
	// re-submitting the source.
	analysisRetryBtn.addEventListener('click', () => submitSource());
	analysisBackBtn.addEventListener('click', () => {
		stopPolling();
		state.batchId = null;
		state.analysis.status = 'idle';
		history.pushState({}, '', window.location.pathname);
		goToStep('upload');
	});

	/* ============================================================
	   C. renderTopicsStep() — topic grid inside step "topics"
	   ============================================================ */
	const topicsGrid = $('#sm-topics-grid');
	const topicsEmpty = $('#sm-topics-empty');
	const topicsEmptyBack = $('#sm-topics-empty-back');
	const topicsSelectedCount = $('#sm-topics-selected-count');
	const topicsTotalCount = $('#sm-topics-total-count');
	const btn2 = $('#sm-btn-2');

	function topicCardHTML(topic) {
		return `
			<label class="sm-topic-card reveal">
				<input type="checkbox" data-topic-id="${topic.id}" ${topic.selected ? 'checked' : ''}>
				<div class="sm-topic-inner">
					<div class="sm-topic-thumb">
						<i class="fa-regular fa-image" aria-hidden="true"></i>
						<span class="sm-topic-time">${topic.start}–${topic.end}</span>
					</div>
					<div class="sm-topic-top">
						<span class="sm-topic-icon"><i class="fa-solid ${topic.icon}"></i></span>
						<span class="sm-topic-duration">${topic.durationLabel}</span>
					</div>
					<span class="sm-topic-check"><i class="fa-solid fa-check"></i></span>
					<span class="sm-topic-name">${escapeHtml(topic.title)}</span>
					<span class="sm-topic-desc">${escapeHtml(topic.description || '')}</span>
				</div>
			</label>
		`;
	}

	function renderTopicsStep() {
		const items = state.topics.items;
		topicsEmpty.classList.toggle('is-hidden', items.length !== 0);
		topicsGrid.hidden = items.length === 0;
		if (items.length === 0) {
			topicsGrid.innerHTML = '';
			updateTopicsCount();
			return;
		}
		topicsTotalCount.textContent = String(items.length);
		topicsGrid.innerHTML = items.map(topicCardHTML).join('');
		$$('input[data-topic-id]', topicsGrid).forEach(input => {
			input.addEventListener('change', () => {
				const topic = items.find(t => t.id === input.dataset.topicId);
				topic.selected = input.checked;
				updateTopicsCount();
			});
		});
		updateTopicsCount();
	}

	function updateTopicsCount() {
		const items = state.topics.items;
		const n = items.filter(t => t.selected).length;
		topicsSelectedCount.textContent = String(n);
		topicsTotalCount.textContent = String(items.length);
		btn2.disabled = n === 0;
	}

	$('#sm-topics-select-all').addEventListener('click', () => {
		state.topics.items.forEach(t => t.selected = true);
		$$('input[data-topic-id]', topicsGrid).forEach(i => i.checked = true);
		updateTopicsCount();
	});
	$('#sm-topics-select-none').addEventListener('click', () => {
		state.topics.items.forEach(t => t.selected = false);
		$$('input[data-topic-id]', topicsGrid).forEach(i => i.checked = false);
		updateTopicsCount();
	});
	topicsEmptyBack.addEventListener('click', () => goToStep('upload'));

	btn2.addEventListener('click', () => { confirmSelection(); });

	/* ============================================================
	   D. renderClipsStep() — step "clips"
	   ============================================================ */
	const clipsGrid = $('#sm-clips-grid');
	const statPending = $('#sm-stat-pending');
	const statApproved = $('#sm-stat-approved');
	const statSkipped = $('#sm-stat-skipped');
	const statFailedWrap = $('#sm-stat-failed-wrap');
	const statFailed = $('#sm-stat-failed');
	const autoModeToggle = $('#sm-auto-mode');
	const completeBanner = $('#sm-complete');
	const openEditorBtn = $('#sm-open-editor');
	const downloadApprovedBtn = $('#sm-download-approved');
	const approveRecommendedBtn = $('#sm-approve-recommended');
	const processingNote = $('#sm-processing-note');

	// clip.overlay stays empty — the real streamer_segments backend has no
	// overlay-text concept (that belongs to the still-mock promo/subtitle
	// system, explicitly out of scope for this pass — see the REAL BATCH
	// FLOW section below), so this field is honestly blank rather than
	// filled with invented copy.
	function makeClipFromTopic(topic, i) {
		return {
			id: 'c' + topic.id,
			topic,
			title: topic.title,
			overlay: '',
			// Kept strictly in the cyan → violet → magenta band (200–340°,
			// +40 for the gradient's second stop) so per-card thumb gradients
			// never drift into gold/orange/brown hues.
			hue: 200 + ((i * 47) % 100),
			status: 'processing', // processing → pending → approved | skipped | failed
			progressPct: 0,
			previewUrl: null,     // filled in by applyBatchClips() once the compose job completes
			downloadUrl: null,    // gates the "Скачать одобренные" batch action
			remoteId: null,       // real generation_jobs id — set by applyBatchClips()
			failReason: null,
			recommended: !!topic.recommended, // real streamer_segments.recommended (see segments_from_analysis — always false until a real scoring heuristic exists)
			rerendering: false,             // transient — subtitle quick-editor rerender in progress
			transcript: null,               // lazily filled by ensureTranscript() on first editor open
			subtitlePresetId: state.accountPreset.activeSubtitlePresetId,
			subtitleOverrides: {},
			renderVersion: 1,
		};
	}

	function buildClips() {
		const selected = state.topics.items.filter(t => t.selected);
		state.clips.items = selected.map((topic, i) => makeClipFromTopic(topic, i));
	}

	function clipCardHTML(clip) {
		const gradient = `linear-gradient(160deg, hsl(${clip.hue} 70% 22%), hsl(${(clip.hue + 40) % 360} 70% 12%))`;

		if (clip.status === 'processing') {
			return `
				<div class="sm-clip-card" id="clip-${clip.id}">
					<div class="sm-clip-thumb" style="background:${gradient}">
						<canvas class="sm-clip-cloth" data-cloth-canvas aria-hidden="true"></canvas>
						<div class="sm-clip-cloth-glare" aria-hidden="true"></div>
						<div class="sm-clip-thumb-topline">
							<span class="sm-clip-thumb-time">${clip.topic.start}</span>
							<span class="sm-clip-thumb-badge is-processing">Обработка</span>
						</div>
					</div>
					<div class="sm-clip-body">
						<div class="sm-clip-processing-label">
							<span>${escapeHtml(clip.topic.title)}</span>
							<span class="sm-clip-processing-pct">${clip.progressPct}%</span>
						</div>
					</div>
				</div>`;
		}

		if (clip.status === 'failed') {
			return `
				<div class="sm-clip-card is-failed" id="clip-${clip.id}">
					<div class="sm-clip-thumb" style="background:${gradient}">
						<div class="sm-clip-thumb-topline">
							<span class="sm-clip-thumb-time">${clip.topic.start}–${clip.topic.end}</span>
							<span class="sm-clip-thumb-badge is-failed">Ошибка</span>
						</div>
						<span class="sm-clip-thumb-placeholder"><i class="fa-solid fa-triangle-exclamation"></i></span>
					</div>
					<div class="sm-clip-body">
						<div class="sm-clip-failed-msg">
							<i class="fa-solid fa-circle-exclamation"></i>
							<span>${escapeHtml(clip.failReason || 'Не удалось обработать клип.')}</span>
						</div>
						<div class="sm-clip-actions">
							<button class="sm-clip-btn sm-clip-btn--retry" type="button" data-retry><i class="fa-solid fa-rotate-right"></i> Повторить</button>
						</div>
					</div>
				</div>`;
		}

		const resolved = clip.status === 'approved' || clip.status === 'skipped';
		const badgeClass = clip.status === 'approved' ? 'is-approved' : clip.status === 'skipped' ? 'is-skipped' : 'is-pending';
		const badgeLabel = clip.status === 'approved' ? 'Одобрено' : clip.status === 'skipped' ? 'В редактор' : 'Проверка';
		const showRecommended = clip.recommended && state.accountPreset.autoReviewMode !== 'manual';
		return `
			<div class="sm-clip-card ${clip.status === 'approved' ? 'is-approved' : ''} ${clip.status === 'skipped' ? 'is-skipped' : ''}" id="clip-${clip.id}">
				<div class="sm-clip-thumb" style="background:${gradient}">
					${clip.rerendering ? `
						<canvas class="sm-clip-cloth" data-cloth-canvas aria-hidden="true"></canvas>
						<div class="sm-clip-thumb-rerender-overlay">
							<span class="sm-clip-thumb-rerender-label">Пересборка субтитров…</span>
						</div>
					` : ''}
					<div class="sm-clip-thumb-topline">
						<span class="sm-clip-thumb-time">${clip.topic.start}–${clip.topic.end}</span>
						<span class="sm-clip-thumb-badge ${badgeClass}">${badgeLabel}</span>
					</div>
					${clip.previewUrl
						? `<button class="sm-clip-thumb-play" type="button" data-play aria-label="Просмотр"><i class="fa-solid fa-play"></i></button>`
						: `<span class="sm-clip-thumb-placeholder"><i class="fa-solid fa-clapperboard"></i></span>`}
					<span class="sm-clip-thumb-overlay" data-overlay-preview>${clip.status === 'skipped' ? '' : escapeHtml(clip.overlay)}</span>
				</div>
				<div class="sm-clip-body">
					${showRecommended ? `<div class="sm-clip-recommended"><i class="fa-regular fa-star"></i> Рекомендуем</div>` : ''}
					<div class="sm-clip-field">
						<label>Название</label>
						<input type="text" data-clip-title value="${escapeAttr(clip.title)}" ${resolved ? 'disabled' : ''}>
					</div>
					<div class="sm-clip-field">
						<label>Оверлей</label>
						<textarea rows="2" data-clip-overlay ${resolved ? 'disabled' : ''}>${escapeHtml(clip.overlay)}</textarea>
					</div>
					<div class="sm-clip-icon-actions">
						<button class="sm-clip-icon-btn" type="button" data-clip-preview ${clip.previewUrl ? '' : 'disabled'} title="Просмотр" aria-label="Просмотр"><i class="fa-solid fa-play" aria-hidden="true"></i></button>
						<button class="sm-clip-icon-btn" type="button" data-clip-download ${clip.downloadUrl ? '' : 'disabled'} title="Скачать" aria-label="Скачать"><i class="fa-solid fa-download" aria-hidden="true"></i></button>
						<button class="sm-clip-icon-btn" type="button" data-clip-subtitles title="Субтитры" aria-label="Субтитры"><i class="fa-solid fa-closed-captioning" aria-hidden="true"></i></button>
						<button class="sm-clip-icon-btn" type="button" data-clip-edit ${clip.remoteId ? '' : 'disabled'} title="Редактировать" aria-label="Редактировать в OpenCut"><i class="fa-solid fa-pen" aria-hidden="true"></i></button>
					</div>
					${resolved ? `
						<div class="sm-clip-resolved">
							<i class="fa-solid ${clip.status === 'approved' ? 'fa-circle-check' : 'fa-arrow-right'}"></i>
							${clip.status === 'approved' ? 'Клип одобрен' : 'Отправлен в редактор без подписей'}
						</div>
					` : `
						<div class="sm-clip-actions">
							<button class="sm-clip-btn sm-clip-btn--approve" type="button" data-approve><i class="fa-solid fa-check"></i> Одобрить</button>
							<button class="sm-clip-btn sm-clip-btn--skip" type="button" data-skip><i class="fa-solid fa-eye-slash"></i> Без подписей</button>
						</div>
					`}
				</div>
			</div>`;
	}

	function renderClipsStep() {
		clipsGrid.innerHTML = state.clips.items.map(clipCardHTML).join('');
		wireClipCard();
		updateStats();
		updateOpenEditorState();
		updateDownloadState();
		updateApproveRecommendedVisibility();
		updateProcessingNote();
	}

	function rerenderClip(clip) {
		const el = document.getElementById('clip-' + clip.id);
		if (!el) return;
		el.outerHTML = clipCardHTML(clip);
		wireClipCard(clip.id);
		updateStats();
		updateOpenEditorState();
		updateDownloadState();
		updateApproveRecommendedVisibility();
		updateProcessingNote();
	}

	function wireClipCard(onlyId) {
		state.clips.items.forEach(clip => {
			if (onlyId && clip.id !== onlyId) return;
			const el = document.getElementById('clip-' + clip.id);
			if (!el) return;
			if (clip.status === 'processing') {
				const clothCanvas = $('[data-cloth-canvas]', el);
				if (clothCanvas) ClothEngine.attach(clothCanvas);
				return;
			}
			if (clip.status === 'failed') {
				const retryBtn = $('[data-retry]', el);
				if (retryBtn) retryBtn.addEventListener('click', () => retryClip(clip));
				return;
			}
			if (clip.rerendering) {
				const clothCanvas = $('[data-cloth-canvas]', el);
				if (clothCanvas) ClothEngine.attach(clothCanvas);
			}
			const titleInput = $('[data-clip-title]', el);
			const overlayInput = $('[data-clip-overlay]', el);
			const preview = $('[data-overlay-preview]', el);
			if (titleInput) titleInput.addEventListener('input', () => { clip.title = titleInput.value; });
			if (overlayInput) overlayInput.addEventListener('input', () => {
				clip.overlay = overlayInput.value;
				if (preview) preview.textContent = overlayInput.value;
			});
			const approveBtn = $('[data-approve]', el);
			const skipBtn = $('[data-skip]', el);
			if (approveBtn) approveBtn.addEventListener('click', () => resolveClip(clip, 'approved'));
			if (skipBtn) skipBtn.addEventListener('click', () => resolveClip(clip, 'skipped'));
			// data-play/download/edit are gated to disabled in clipCardHTML
			// when their backing url/id is missing (always, in this mock
			// build) — a disabled <button> can't fire click, so nothing to
			// wire for them yet. Subtitles has no such gate.
			const subtitlesBtn = $('[data-clip-subtitles]', el);
			if (subtitlesBtn) subtitlesBtn.addEventListener('click', () => openSubtitleEditor(clip));
		});
	}

	function resolveClip(clip, status) {
		if (clip.status === 'approved' || clip.status === 'skipped') return;
		clip.status = status;
		rerenderClip(clip);
		checkComplete();
	}

	// Re-composing a single failed segment isn't part of this pass's
	// backend (see the REAL BATCH FLOW section below — POST .../selection
	// creates each segment's compose job exactly once, and there's no
	// endpoint to replace just one); honest refusal here beats a fake
	// success animation.
	function retryClip(clip) {
		showToast('Повтор одного клипа пока не поддерживается — запустите обработку заново из шага «Источник».', 'error');
	}

	function updateStats() {
		const items = state.clips.items;
		const pending = items.filter(c => c.status === 'pending' || c.status === 'processing').length;
		const approved = items.filter(c => c.status === 'approved').length;
		const skipped = items.filter(c => c.status === 'skipped').length;
		const failed = items.filter(c => c.status === 'failed').length;
		statPending.textContent = String(pending);
		statApproved.textContent = String(approved);
		statSkipped.textContent = String(skipped);
		statFailed.textContent = String(failed);
		statFailedWrap.hidden = failed === 0;
	}

	function checkComplete() {
		const items = state.clips.items;
		const allDone = items.length > 0 && items.every(c => c.status === 'approved' || c.status === 'skipped');
		completeBanner.classList.toggle('is-visible', allDone);
	}

	// Gates "Открыть в редакторе" on a *real* backend id — the mock local
	// `clip.id` (used for DOM/state bookkeeping) never counts, so this
	// button stays disabled until job creation actually exists.
	function updateOpenEditorState() {
		const hasRealApproved = state.clips.items.some(c => c.status === 'approved' && c.remoteId);
		openEditorBtn.disabled = !hasRealApproved;
	}

	// Same real-backend gate as updateOpenEditorState(), on downloadUrl
	// instead of remoteId — mock never sets either, so both stay disabled
	// until job creation actually exists.
	function updateDownloadState() {
		const hasDownloadable = state.clips.items.some(c => c.status === 'approved' && c.downloadUrl);
		downloadApprovedBtn.disabled = !hasDownloadable;
	}

	// "Одобрить рекомендованные" only makes sense once auto-review is
	// something other than fully manual, and only while there's still a
	// pending recommended clip to act on.
	function updateApproveRecommendedVisibility() {
		const relevant = state.accountPreset.autoReviewMode !== 'manual';
		const hasPendingRecommended = state.clips.items.some(c => c.status === 'pending' && c.recommended);
		approveRecommendedBtn.hidden = !(relevant && hasPendingRecommended);
	}

	// A real batch backs this now (see the REAL BATCH FLOW section below)
	// — SONYA keeps composing clips server-side regardless of this tab, so
	// it's honest to say so once at least one clip is still processing.
	function updateProcessingNote() {
		const stillProcessing = state.clips.items.some(c => c.status === 'processing');
		processingNote.classList.toggle('is-hidden', !stillProcessing);
		processingNote.innerHTML = stillProcessing
			? `<i class="fa-solid fa-gear sm-processing-note-icon"></i> Можно закрыть вкладку — SONYA продолжит обработку.`
			: '';
	}

	autoModeToggle.addEventListener('change', () => {
		state.clips.autoMode = autoModeToggle.checked;
		if (state.clips.autoMode) {
			showToast('Авто-режим включён — SONYA одобрит рекомендованные клипы', 'info');
			state.clips.items.filter(c => c.status === 'pending' && c.recommended).forEach((clip, i) => {
				setTimeout(() => resolveClip(clip, 'approved'), i * 160);
			});
		}
	});

	approveRecommendedBtn.addEventListener('click', () => {
		state.clips.items.filter(c => c.status === 'pending' && c.recommended).forEach((clip, i) => {
			setTimeout(() => resolveClip(clip, 'approved'), i * 120);
		});
	});

	$('#sm-approve-all').addEventListener('click', () => {
		state.clips.items.filter(c => c.status === 'pending').forEach((clip, i) => {
			setTimeout(() => resolveClip(clip, 'approved'), i * 120);
		});
	});
	$('#sm-skip-all').addEventListener('click', () => {
		state.clips.items.filter(c => c.status === 'pending').forEach((clip, i) => {
			setTimeout(() => resolveClip(clip, 'skipped'), i * 120);
		});
	});

	/* ============================================================
	   REAL BATCH FLOW — POST /api/streamer/batches -> analyze once ->
	   segments -> POST .../selection -> N real compose jobs -> real
	   clips. Backed by scripts/streamer_routes.py. Replaces the old
	   startAnalysis()/finishAnalysis()/buildClips()/runClipProcessing()
	   mock timers above with real submit + poll calls, reusing every
	   render*Step()/clipCardHTML()/topicCardHTML() function unchanged —
	   only the data source changed. smApiFetch (see the Telegram section
	   below) is used the same way here.

	   Still explicitly mock/unwired in this pass (see module docstring in
	   scripts/streamer_routes.py for the authoritative list): subtitle
	   quick-editor rerender, promo assets, "Открыть в редакторе"/OpenCut,
	   single-clip retry (see retryClip() above), and any Telegram
	   completion notification.
	   ============================================================ */

	function formatTimecode(seconds) {
		const s = Math.max(0, Math.round(Number(seconds) || 0));
		return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
	}

	function segmentToTopic(seg, index) {
		return {
			id: seg.segment_id,
			start: formatTimecode(seg.start_sec),
			end: formatTimecode(seg.start_sec + seg.duration_sec),
			title: seg.title,
			description: seg.description || '',
			icon: 'fa-clapperboard', // real segments carry no per-topic icon — one neutral default for all
			selected: seg.selected !== false,
			origIndex: index,
			durationLabel: formatDuration(seg.duration_sec),
			recommended: !!seg.recommended,
		};
	}

	let pollTimer = null;
	function stopPolling() {
		if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
	}

	const POLL_INTERVAL_MS = 2500;
	const _LOADING_STATUSES = ['queued', 'ingesting', 'analyzing'];
	const _CLIP_STEP_STATUSES = ['generating', 'ready', 'partially_failed'];

	let _submittingSource = false;

	async function submitSource() {
		// Guards the common double-click case directly (ignore a re-entrant
		// call while one is already in flight); the Idempotency-Key below
		// is the server-side backstop for everything this can't catch on
		// its own (a network-level retry of a request already sent, or a
		// second tab/click that slips past this in-memory flag).
		if (_submittingSource) return;

		const formData = new FormData();
		if (state.source.mode === 'url' && state.source.url.trim()) {
			formData.append('source_type', 'url');
			formData.append('url', state.source.url.trim());
		} else if (state.source.mode === 'file' && state.source.file) {
			formData.append('source_type', 'file');
			formData.append('file', state.source.file);
		} else {
			return;
		}

		_submittingSource = true;
		stopPolling();
		state.batchId = null;
		goToStep('topics');
		state.analysis.status = 'running';
		state.analysis.stageLabel = ANALYSIS_STAGE_LABELS.queued;
		state.analysis.progress = 15;
		state.analysis.error = null;
		renderAnalysisState();
		const loadingCloth = $('[data-cloth-canvas]', loadingEl);
		if (loadingCloth) ClothEngine.attach(loadingCloth);

		const idempotencyKey = _getOrCreateBatchIdempotencyKey();
		let resp;
		try {
			resp = await smApiFetch('/api/streamer/batches', {
				method: 'POST', headers: { 'Idempotency-Key': idempotencyKey }, body: formData,
			});
			// A network-level failure (resp === null) means the client
			// doesn't know whether the server actually received this
			// request -- KEEP the key so a retry of this same attempt
			// converges server-side on one batch. Any real response
			// (success or a definitive error) settles this attempt --
			// clear it so the next click is always a new logical run.
			if (resp) _clearBatchIdempotencyKey();

			if (!resp || !resp.ok) {
				let message = 'Не удалось запустить обработку. Проверьте источник и попробуйте снова.';
				if (resp && resp.status === 402) {
					message = 'Бесплатная генерация уже использована. Оформите SONYA Pro.';
				} else if (resp) {
					try {
						const errBody = await resp.json();
						if (errBody && errBody.detail && errBody.detail.message) message = errBody.detail.message;
					} catch (_) { /* non-JSON error body — keep the default message */ }
				}
				state.analysis.status = 'error';
				state.analysis.error = message;
				renderAnalysisState();
				showToast('Не удалось запустить обработку', 'error');
				return;
			}

			const data = await resp.json();
			state.batchId = data.batch_id;
			history.pushState({ batchId: data.batch_id }, '', `?batch=${encodeURIComponent(data.batch_id)}`);
			pollBatchUntilSelectable();
		} finally {
			_submittingSource = false;
		}
	}

	function pollBatchUntilSelectable() {
		stopPolling();
		const myBatchId = state.batchId;
		const tick = async () => {
			if (state.batchId !== myBatchId) return; // superseded by a newer batch
			const resp = await smApiFetch(`/api/streamer/batches/${encodeURIComponent(myBatchId)}`);
			if (state.batchId !== myBatchId) return;
			if (!resp || !resp.ok) {
				pollTimer = setTimeout(tick, POLL_INTERVAL_MS);
				return;
			}
			const data = await resp.json();

			if (_LOADING_STATUSES.includes(data.status)) {
				state.analysis.stageLabel = ANALYSIS_STAGE_LABELS[data.status] || ANALYSIS_STAGE_LABELS.queued;
				state.analysis.progress = Math.min(90, state.analysis.progress + 8);
				renderAnalysisState();
				pollTimer = setTimeout(tick, POLL_INTERVAL_MS);
				return;
			}
			if (data.status === 'awaiting_selection') {
				state.topics.items = (data.segments || []).map(segmentToTopic);
				state.analysis.status = 'done';
				renderAnalysisState();
				renderTopicsStep();
				showToast(`Анализ завершён — найдено тем: ${state.topics.items.length}`, 'success');
				return;
			}
			if (_CLIP_STEP_STATUSES.includes(data.status)) {
				// Rehydration landed mid-flight (selection already happened,
				// e.g. this poll was still running from before a reload) —
				// skip straight to the clips step.
				enterClipsStepFromBatch(data);
				return;
			}
			if (data.status === 'failed' || data.status === 'cancelled') {
				state.analysis.status = 'error';
				state.analysis.error = data.error === 'FREE_PLAN_USED'
					? 'Бесплатная генерация уже использована. Оформите SONYA Pro.'
					: 'Не удалось скачать или распознать видео. Проверьте ссылку/файл и попробуйте снова.';
				renderAnalysisState();
				showToast('Анализ стрима не удался', 'error');
				return;
			}
			pollTimer = setTimeout(tick, POLL_INTERVAL_MS); // unknown status — keep polling defensively
		};
		tick();
	}

	async function confirmSelection() {
		buildClips();
		goToStep('clips');
		state.clips.autoMode = state.accountPreset.autoReviewMode === 'auto';
		autoModeToggle.checked = state.clips.autoMode;
		renderClipsStep();

		const segmentIds = state.clips.items.map(c => c.topic.id);
		const resp = await smApiFetch(`/api/streamer/batches/${encodeURIComponent(state.batchId)}/selection`, {
			method: 'POST',
			body: JSON.stringify({ segment_ids: segmentIds }),
		});
		if (!resp || !resp.ok) {
			showToast('Не удалось запустить создание клипов', 'error');
			return;
		}
		pollBatchUntilDone();
	}

	function enterClipsStepFromBatch(data) {
		const clipSegmentIds = new Set((data.clips || []).map(c => c.segment_id));
		state.topics.items = (data.segments || []).map(segmentToTopic);
		state.clips.items = state.topics.items
			.filter(t => clipSegmentIds.has(t.id))
			.map((t, i) => makeClipFromTopic(t, i));
		state.clips.autoMode = state.accountPreset.autoReviewMode === 'auto';
		autoModeToggle.checked = state.clips.autoMode;
		goToStep('clips');
		renderClipsStep();
		applyBatchClips(data);
		if (data.status === 'generating') pollBatchUntilDone();
	}

	function pollBatchUntilDone() {
		stopPolling();
		const myBatchId = state.batchId;
		const tick = async () => {
			if (state.batchId !== myBatchId) return;
			const resp = await smApiFetch(`/api/streamer/batches/${encodeURIComponent(myBatchId)}`);
			if (state.batchId !== myBatchId) return;
			if (!resp || !resp.ok) {
				pollTimer = setTimeout(tick, POLL_INTERVAL_MS);
				return;
			}
			const data = await resp.json();
			applyBatchClips(data);
			if (data.status === 'generating') {
				pollTimer = setTimeout(tick, POLL_INTERVAL_MS);
			}
			// ready / partially_failed / failed: terminal, stop polling.
		};
		tick();
	}

	// Merges real generation_jobs status per segment into state.clips.items
	// — never touches a clip the user has already manually approved/
	// skipped (that's a local review decision, not something a later poll
	// tick should silently revert).
	function applyBatchClips(data) {
		const byId = new Map((data.clips || []).map(c => [c.segment_id, c]));
		state.clips.items.forEach(clip => {
			if (clip.status === 'approved' || clip.status === 'skipped') return;
			const backendClip = byId.get(clip.topic.id);
			if (!backendClip) return;
			clip.remoteId = backendClip.job_id;

			let nextStatus = 'processing';
			if (backendClip.status === 'completed') nextStatus = 'pending';
			else if (backendClip.status === 'failed') nextStatus = 'failed';

			if (nextStatus === 'pending') {
				clip.progressPct = 100;
				clip.previewUrl = backendClip.previewUrl || null;
				clip.downloadUrl = backendClip.downloadUrl || null;
			} else if (nextStatus === 'failed') {
				clip.failReason = backendClip.error || 'GPU-воркер не смог обработать сегмент.';
			}

			if (clip.status !== nextStatus) {
				clip.status = nextStatus;
				rerenderClip(clip);
				// Auto-review only ever auto-approves *recommended* clips
				// (see AUTO REVIEW in the file header) — everything else
				// still lands in "pending" for manual review.
				if (nextStatus === 'pending' && state.clips.autoMode && clip.recommended) {
					resolveClip(clip, 'approved');
				}
			}
		});
		updateProcessingNote();
	}

	// ?batch=<id> rehydration — restores the correct step on page load
	// without a reload, per the batch's real server-side status. Silently
	// falls back to the empty source step for a stale/foreign/deleted
	// batch id rather than surfacing an error for what is, from the
	// user's perspective, just an old link.
	async function rehydrateFromUrl() {
		const batchId = new URLSearchParams(window.location.search).get('batch');
		if (!batchId) return;

		const resp = await smApiFetch(`/api/streamer/batches/${encodeURIComponent(batchId)}`);
		if (!resp || !resp.ok) return;
		const data = await resp.json();
		state.batchId = batchId;

		if (_LOADING_STATUSES.includes(data.status)) {
			goToStep('topics');
			state.analysis.status = 'running';
			state.analysis.stageLabel = ANALYSIS_STAGE_LABELS[data.status] || ANALYSIS_STAGE_LABELS.queued;
			state.analysis.progress = 30;
			renderAnalysisState();
			const loadingCloth = $('[data-cloth-canvas]', loadingEl);
			if (loadingCloth) ClothEngine.attach(loadingCloth);
			pollBatchUntilSelectable();
			return;
		}
		if (data.status === 'awaiting_selection') {
			state.topics.items = (data.segments || []).map(segmentToTopic);
			state.analysis.status = 'done';
			goToStep('topics');
			renderAnalysisState();
			renderTopicsStep();
			return;
		}
		if (_CLIP_STEP_STATUSES.includes(data.status)) {
			enterClipsStepFromBatch(data);
			return;
		}
		if (data.status === 'failed' || data.status === 'cancelled') {
			goToStep('topics');
			state.analysis.status = 'error';
			state.analysis.error = 'Обработка этого стрима не удалась. Запустите заново из шага «Источник».';
			renderAnalysisState();
		}
	}

	/* ============================================================
	   E. Streamer Preset drawer — account-level config (subtitles /
	   promo / auto-review / Telegram), reached from the summary row
	   on step "Темы" (#sm-preset-configure). One drawer, four tabs,
	   not four pages — see streamer-mode.css DRAWER SYSTEM.
	   ============================================================ */
	const presetConfigureBtn = $('#sm-preset-configure');
	const presetDrawerOverlay = $('#sm-preset-drawer-overlay');
	const presetDrawerClose = $('#sm-preset-drawer-close');
	const presetDrawerDone = $('#sm-preset-drawer-done');
	const presetDrawerTabs = $$('.sm-drawer-tab', presetDrawerOverlay);
	const presetDrawerPanels = $$('.sm-drawer-panel', presetDrawerOverlay);
	const subpresetGrid = $('#sm-subpreset-grid');
	const subpresetControls = $('#sm-subpreset-controls');
	const promoList = $('#sm-promo-list');
	const promoEmpty = $('#sm-promo-empty');
	const promoFileInput = $('#sm-promo-file-input');
	const autoreviewOptions = $('#sm-autoreview-options');
	const telegramNotifyToggle = $('#sm-telegram-notify-toggle');
	const telegramStatusEl = $('#sm-telegram-status');

	const SIZE_OPTIONS = [{ id: 's', label: 'S' }, { id: 'm', label: 'M' }, { id: 'l', label: 'L' }];
	const POSITION_OPTIONS = [{ id: 'top', label: 'Верх' }, { id: 'bottom', label: 'Низ' }];
	const MAXLINES_OPTIONS = [{ id: 1, label: '1 строка' }, { id: 2, label: '2 строки' }];

	function getSubtitlePreset(id) {
		return state.accountPreset.subtitlePresets.find(p => p.id === id) || state.accountPreset.subtitlePresets[0];
	}

	// Sample-caption preview inside each preset card — purely illustrative
	// CSS, no rendering pipeline involved (see .sm-subpreset-preview).
	function subtitlePresetCardHTML(preset, selectedId, inputName) {
		return `
			<label class="sm-subpreset-card">
				<input type="radio" name="${inputName}" value="${preset.id}" ${preset.id === selectedId ? 'checked' : ''}>
				<div class="sm-subpreset-inner">
					<div class="sm-subpreset-preview" data-sub-position="${preset.position}" data-sub-size="${preset.size}">
						<span class="sm-subpreset-caption">Привет, чат</span>
					</div>
					<div class="sm-subpreset-name">${escapeHtml(preset.name)}</div>
				</div>
			</label>`;
	}

	// Shared by both the account preset drawer (edits a preset object
	// directly) and the subtitle quick editor (edits clip.subtitleOverrides
	// through onSelect/onChange) — same component, different write target.
	function renderSubtitlePresetPicker(container, presets, selectedId, inputName, onSelect) {
		container.innerHTML = presets.map(p => subtitlePresetCardHTML(p, selectedId, inputName)).join('') +
			`<label class="sm-subpreset-card sm-subpreset-add">
				<input type="radio" name="${inputName}" value="__add__">
				<div class="sm-subpreset-inner"><i class="fa-solid fa-plus"></i></div>
			</label>`;
		$$('input', container).forEach(input => {
			input.addEventListener('change', () => {
				if (input.value === '__add__') {
					const base = getSubtitlePreset(selectedId);
					const clone = { ...base, id: 'sub-custom-' + Date.now(), name: 'Свой пресет', isDefault: false };
					state.accountPreset.subtitlePresets.push(clone);
					onSelect(clone.id);
					return;
				}
				onSelect(input.value);
			});
		});
	}

	function segRowHTML(label, options, activeId, dataAttr) {
		return `
			<div class="sm-seg-row">
				<span class="sm-seg-label">${escapeHtml(label)}</span>
				<div class="sm-seg" data-seg="${dataAttr}">
					${options.map(o => `<button type="button" class="sm-seg-btn ${o.id === activeId ? 'is-active' : ''}" data-seg-value="${o.id}">${escapeHtml(o.label)}</button>`).join('')}
				</div>
			</div>`;
	}

	// Size/position/max-lines/highlight controls for a subtitle preset —
	// reused for both an account preset (onChange mutates it directly)
	// and a clip's override view (onChange writes into subtitleOverrides).
	function renderPresetControls(container, config, onChange) {
		container.innerHTML = `
			${segRowHTML('Размер', SIZE_OPTIONS, config.size, 'size')}
			${segRowHTML('Позиция', POSITION_OPTIONS, config.position, 'position')}
			${segRowHTML('Строки', MAXLINES_OPTIONS, config.maxLines, 'maxLines')}
			<div class="sm-seg-toggle-row">
				<span>Подсветка активного слова</span>
				<label class="toggle-switch">
					<input type="checkbox" data-seg="activeWordHighlight" ${config.activeWordHighlight ? 'checked' : ''}>
					<span class="toggle-slider"></span>
				</label>
			</div>`;
		$$('.sm-seg-btn', container).forEach(btn => {
			btn.addEventListener('click', () => {
				const field = btn.closest('[data-seg]').dataset.seg;
				let value = btn.dataset.segValue;
				if (field === 'maxLines') value = Number(value);
				onChange(field, value);
			});
		});
		const highlightInput = $('[data-seg="activeWordHighlight"]', container);
		if (highlightInput) highlightInput.addEventListener('change', () => onChange('activeWordHighlight', highlightInput.checked));
	}

	function renderSubtitlesTab() {
		const selectedId = state.accountPreset.activeSubtitlePresetId;
		renderSubtitlePresetPicker(subpresetGrid, state.accountPreset.subtitlePresets, selectedId, 'sm-subpreset', (id) => {
			state.accountPreset.activeSubtitlePresetId = id;
			renderSubtitlesTab();
		});
		const preset = getSubtitlePreset(selectedId);
		renderPresetControls(subpresetControls, preset, (field, value) => {
			preset[field] = value;
			renderSubtitlesTab();
		});
	}

	/* ── Promo / Ads ── */
	function promoIconForType(type) {
		return type === 'video' ? 'fa-solid fa-clapperboard' : type === 'image' ? 'fa-regular fa-image' : 'fa-solid fa-layer-group';
	}

	function promoRowHTML(asset) {
		const thumb = asset.thumbnailUrl
			? `<img src="${escapeAttr(asset.thumbnailUrl)}" alt="">`
			: `<i class="${promoIconForType(asset.type)}" aria-hidden="true"></i>`;
		return `
			<div class="sm-promo-row" data-promo-id="${asset.id}">
				<div class="sm-promo-thumb">${thumb}</div>
				<div class="sm-promo-info">
					<div class="sm-promo-name-row">
						<span class="sm-promo-name">${escapeHtml(asset.name)}</span>
						<span class="sm-promo-type">${asset.type === 'video' ? 'Видео' : asset.type === 'image' ? 'Изображение' : 'Оверлей'}</span>
					</div>
					<div class="sm-promo-controls">
						<select class="sm-promo-rule-select" data-promo-rule>
							${PROMO_RULES.map(r => `<option value="${r.id}" ${r.id === asset.rule ? 'selected' : ''}>${escapeHtml(r.label)}</option>`).join('')}
						</select>
						<input type="number" class="sm-promo-duration-input" data-promo-duration min="1" max="30" value="${asset.overlaySeconds || 5}" ${asset.rule === 'overlay' ? '' : 'hidden'}>
						${asset.rule === 'overlay' ? '<span class="sm-promo-name" style="font-size:11px">сек</span>' : ''}
					</div>
				</div>
				<button class="sm-promo-remove" type="button" data-promo-remove aria-label="Удалить"><i class="fa-solid fa-xmark"></i></button>
			</div>`;
	}

	function renderPromoTab() {
		const assets = state.accountPreset.promoAssets;
		promoEmpty.classList.toggle('is-hidden', assets.length !== 0);
		promoList.innerHTML = assets.map(promoRowHTML).join('');
		$$('[data-promo-id]', promoList).forEach(row => {
			const id = row.dataset.promoId;
			const asset = assets.find(a => a.id === id);
			$('[data-promo-rule]', row).addEventListener('change', (e) => {
				asset.rule = e.target.value;
				renderPromoTab();
			});
			const durationInput = $('[data-promo-duration]', row);
			if (durationInput) durationInput.addEventListener('input', (e) => {
				asset.overlaySeconds = Number(e.target.value) || 5;
			});
			$('[data-promo-remove]', row).addEventListener('click', () => {
				state.accountPreset.promoAssets = state.accountPreset.promoAssets.filter(a => a.id !== id);
				renderPromoTab();
			});
		});
	}

	function addPromoAsset(file) {
		if (!file) return;
		const type = file.type.startsWith('image/') ? 'image' : file.type.startsWith('video/') ? 'video' : 'overlay';
		const asset = {
			id: 'promo-' + Date.now(),
			name: file.name,
			type,
			// Local-only preview — never uploaded anywhere, just an object
			// URL for this browser tab (see NOT DONE YET: no real media
			// backend yet).
			thumbnailUrl: type === 'image' ? URL.createObjectURL(file) : null,
			rule: 'suggest',
			overlaySeconds: 5,
		};
		state.accountPreset.promoAssets.push(asset);
		renderPromoTab();
		showToast('Материал добавлен (демо)', 'success');
	}
	promoFileInput.addEventListener('change', e => { addPromoAsset(e.target.files[0]); promoFileInput.value = ''; });

	/* ── Auto-review ── */
	const AUTO_REVIEW_OPTIONS = [
		{ id: 'manual', name: 'Проверять вручную', desc: 'Каждый клип ждёт вашего решения — одобрить или пропустить.' },
		{ id: 'recommend', name: 'SONYA рекомендует', desc: 'Лучшие клипы отмечаются меткой «Рекомендуем», решение всё равно за вами.' },
		{ id: 'auto', name: 'Автоодобрение рекомендованных', desc: 'Рекомендованные клипы одобряются сами, остальные ждут проверки.' },
	];

	function renderAutoTab() {
		autoreviewOptions.innerHTML = AUTO_REVIEW_OPTIONS.map(o => `
			<label class="sm-autoreview-option">
				<input type="radio" name="sm-autoreview" value="${o.id}" ${o.id === state.accountPreset.autoReviewMode ? 'checked' : ''}>
				<div class="sm-autoreview-option-inner">
					<span class="sm-autoreview-dot"></span>
					<div class="sm-autoreview-text">
						<span class="sm-autoreview-name">${escapeHtml(o.name)}</span>
						<span class="sm-autoreview-desc">${escapeHtml(o.desc)}</span>
					</div>
				</div>
			</label>
		`).join('');
		$$('input', autoreviewOptions).forEach(input => {
			input.addEventListener('change', () => {
				state.accountPreset.autoReviewMode = input.value;
			});
		});
	}

	/* ── Telegram ──
	   Real backend, not mock — see scripts/telegram_routes.py. Everything
	   else in this file is still mock (no real job creation yet); this is
	   the one deliberate exception, per the account-linking backend pass.
	   Notify-on-completion delivery itself is NOT implemented yet (no
	   streamer_batches sender exists) — telegramNotifyEnabled here is
	   still just a local UI preference, not wired to anything server-side. */
	const SONYA_API_BASE = window.SONYA_API_BASE || '';
	let awaitingTelegramLink = false;

	async function smApiFetch(path, options = {}) {
		try {
			return await fetch(SONYA_API_BASE + path, {
				credentials: 'include', // send the HttpOnly sonya_session cookie
				headers: { 'Content-Type': 'application/json' },
				...options,
			});
		} catch (err) {
			console.warn('[streamer-mode] api_fetch_failed path=' + path, err);
			return null;
		}
	}

	/* Same pattern as auth.js's _getOrCreateJobIdempotencyKey() /
	   clearJobIdempotencyKey() for the plain upload flow — duplicated here
	   rather than shared, since streamer-mode.html doesn't load auth.js.
	   Held across a network-level failure (smApiFetch returned null — the
	   client doesn't know whether the server actually received the
	   request) so a retry of THIS SAME submit attempt converges, server-
	   side, on the one batch already reserved for it (see POST
	   /api/streamer/batches' Idempotency-Key handling in
	   scripts/streamer_routes.py). Cleared the moment any real HTTP
	   response comes back (success or a definitive error) — that means
	   this attempt is settled, so the next click (including a manual
	   "retry" after a failed batch) is always a new logical run, not a
	   replay of the failed one. */
	let _batchIdempotencyKey = null;
	function _getOrCreateBatchIdempotencyKey() {
		if (!_batchIdempotencyKey) {
			_batchIdempotencyKey = (crypto.randomUUID ? crypto.randomUUID() : `sm-${Date.now()}-${Math.random().toString(36).slice(2)}`);
		}
		return _batchIdempotencyKey;
	}
	function _clearBatchIdempotencyKey() {
		_batchIdempotencyKey = null;
	}

	async function fetchTelegramStatus() {
		const resp = await smApiFetch('/api/telegram/status');
		if (!resp || !resp.ok) return; // not logged in yet, or a transient error — leave state as-is
		const data = await resp.json();
		state.accountPreset.telegramLinked = !!data.linked;
		state.accountPreset.telegramLinkedAt = data.linked_at || null;
		renderTelegramTab();
	}

	function renderTelegramTab() {
		telegramNotifyToggle.checked = state.accountPreset.telegramNotifyEnabled;
		telegramStatusEl.classList.toggle('is-linked', state.accountPreset.telegramLinked);
		telegramStatusEl.innerHTML = state.accountPreset.telegramLinked
			? `<span class="sm-telegram-status-text">
					<span class="sm-telegram-status-dot"></span>
					<span>
						<span class="sm-telegram-status-title">Telegram подключён</span>
						<span class="sm-telegram-status-sub">@sonya_group_bot сообщит, когда обработка стрима будет готова.</span>
					</span>
				</span>
			   <button class="sm-link-btn" type="button" id="sm-telegram-unlink">Отключить</button>`
			: `<span class="sm-telegram-status-text"><span class="sm-telegram-status-dot"></span> Telegram не подключён</span>
			   <button class="btn-secondary sm-btn-sm" type="button" id="sm-telegram-link">Подключить @sonya_group_bot</button>`;
		const linkBtn = $('#sm-telegram-link', telegramStatusEl);
		if (linkBtn) linkBtn.addEventListener('click', requestTelegramLink);
		const unlinkBtn = $('#sm-telegram-unlink', telegramStatusEl);
		if (unlinkBtn) unlinkBtn.addEventListener('click', unlinkTelegram);
	}

	async function requestTelegramLink() {
		// window.open() must happen synchronously, as the very first thing
		// in this handler, before any `await` — calling it AFTER an awaited
		// fetch risks popup blockers (Safari especially) no longer crediting
		// the call to the click's own user gesture and silently discarding
		// it. The blank tab gets its real destination assigned once the
		// token request resolves.
		const popup = window.open('about:blank', '_blank', 'noopener');
		const popupBlocked = !popup || popup.closed || typeof popup.closed === 'undefined';

		const resp = await smApiFetch('/api/telegram/link-token', { method: 'POST' });
		if (!resp || !resp.ok) {
			if (popup && !popup.closed) popup.close();
			showToast('Не удалось получить ссылку для подключения Telegram', 'error');
			return;
		}
		const data = await resp.json();
		awaitingTelegramLink = true;

		if (popupBlocked) {
			// The synchronous open was already blocked (e.g. popups
			// disabled entirely) — no silent failure: navigate this tab
			// there directly rather than leaving the click with no visible
			// effect at all.
			window.location.href = data.deep_link;
			return;
		}
		popup.location = data.deep_link;
	}

	async function unlinkTelegram() {
		const resp = await smApiFetch('/api/telegram/unlink', { method: 'POST' });
		if (!resp || !resp.ok) {
			showToast('Не удалось отключить Telegram', 'error');
			return;
		}
		state.accountPreset.telegramLinked = false;
		state.accountPreset.telegramLinkedAt = null;
		renderTelegramTab();
	}

	// "После возврата/фокуса страницы: GET /api/telegram/status" — the
	// user leaves this tab to hit Start in Telegram, then comes back.
	function maybeRefreshTelegramStatus() {
		if (!awaitingTelegramLink) return;
		awaitingTelegramLink = false;
		fetchTelegramStatus();
	}
	document.addEventListener('visibilitychange', () => {
		if (document.visibilityState === 'visible') maybeRefreshTelegramStatus();
	});
	window.addEventListener('focus', maybeRefreshTelegramStatus);

	telegramNotifyToggle.addEventListener('change', () => {
		state.accountPreset.telegramNotifyEnabled = telegramNotifyToggle.checked;
	});

	/* ── Drawer open/close/tabs ── */
	function switchPresetTab(tab) {
		presetDrawerActiveTab = tab;
		presetDrawerTabs.forEach(t => {
			const active = t.dataset.presetTab === tab;
			t.classList.toggle('is-active', active);
			t.setAttribute('aria-selected', String(active));
		});
		presetDrawerPanels.forEach(p => p.classList.toggle('is-active', p.dataset.presetPanel === tab));
	}
	function openPresetDrawer(tab) {
		switchPresetTab(tab || presetDrawerActiveTab);
		renderSubtitlesTab();
		renderPromoTab();
		renderAutoTab();
		renderTelegramTab();
		fetchTelegramStatus(); // refresh from the real backend every time the drawer opens
		presetDrawerOverlay.classList.remove('is-hidden');
	}
	function closePresetDrawer() {
		presetDrawerOverlay.classList.add('is-hidden');
		// Auto-review mode chosen in the drawer changes whether the
		// "Рекомендуем" tag and the recommended bulk action show — only
		// relevant once there are actual clip cards on screen to update.
		if (state.clips.items.length) {
			state.clips.items.forEach(c => { if (document.getElementById('clip-' + c.id)) rerenderClip(c); });
			updateApproveRecommendedVisibility();
		}
	}
	presetConfigureBtn.addEventListener('click', () => openPresetDrawer('subtitles'));
	presetDrawerTabs.forEach(t => t.addEventListener('click', () => switchPresetTab(t.dataset.presetTab)));
	presetDrawerClose.addEventListener('click', closePresetDrawer);
	presetDrawerDone.addEventListener('click', closePresetDrawer);
	presetDrawerOverlay.addEventListener('click', (e) => { if (e.target === presetDrawerOverlay) closePresetDrawer(); });
	document.addEventListener('keydown', (e) => {
		if (e.key !== 'Escape') return;
		if (!presetDrawerOverlay.classList.contains('is-hidden')) closePresetDrawer();
		if (!subtitleDrawerOverlay.classList.contains('is-hidden')) closeSubtitleEditor();
	});

	/* ============================================================
	   F. Subtitle quick editor — per-clip drawer opened from the
	   clip card's "Субтитры" icon action. NOT OpenCut: only touches
	   transcript text/line-breaks and the subtitle preset/overrides
	   for this one clip, then triggers a scoped mock re-render.
	   ============================================================ */
	const subtitleDrawerOverlay = $('#sm-subtitle-drawer-overlay');
	const subtitleDrawerClose = $('#sm-subtitle-drawer-close');
	const subtitleDrawerTitle = $('#sm-subtitle-drawer-title');
	const transcriptList = $('#sm-transcript-list');
	const clipSubpresetGrid = $('#sm-clip-subpreset-grid');
	const clipSubpresetControls = $('#sm-clip-subpreset-controls');
	const subtitleRerenderBtn = $('#sm-subtitle-rerender');
	const subtitleRerenderStatus = $('#sm-subtitle-rerender-status');

	// Lazily mock-generates a transcript the first time a clip's editor
	// opens, split evenly across the topic's real timespan — then it's
	// stored on the clip so further edits persist across drawer opens.
	function ensureTranscript(clip) {
		if (clip.transcript) return clip.transcript;
		const words = (clip.overlay + ' ' + clip.title).split(/\s+/).filter(Boolean);
		const startSec = timecodeToSeconds(clip.topic.start);
		const endSec = timecodeToSeconds(clip.topic.end);
		const lineCount = Math.min(4, Math.max(2, Math.ceil(words.length / 3)));
		const chunkSize = Math.ceil(words.length / lineCount) || 1;
		const spanPerLine = (endSec - startSec) / lineCount;
		const lines = [];
		for (let i = 0; i < lineCount; i++) {
			const text = words.slice(i * chunkSize, (i + 1) * chunkSize).join(' ') || '…';
			lines.push({
				start: formatDuration(startSec + i * spanPerLine),
				end: formatDuration(startSec + (i + 1) * spanPerLine),
				text,
			});
		}
		clip.transcript = lines;
		return lines;
	}

	function renderTranscriptList(clip) {
		const lines = ensureTranscript(clip);
		transcriptList.innerHTML = lines.map((line, i) => `
			<div class="sm-transcript-line" data-line-index="${i}">
				<span class="sm-transcript-time">${line.start}–${line.end}</span>
				<textarea class="sm-transcript-text" rows="1" data-transcript-text>${escapeHtml(line.text)}</textarea>
				<div class="sm-transcript-line-actions">
					<button class="sm-transcript-line-btn" type="button" data-transcript-split title="Разбить строку" aria-label="Разбить строку"><i class="fa-solid fa-scissors"></i></button>
					<button class="sm-transcript-line-btn" type="button" data-transcript-merge ${i === lines.length - 1 ? 'disabled' : ''} title="Объединить со следующей" aria-label="Объединить со следующей строкой"><i class="fa-solid fa-down-long"></i></button>
				</div>
			</div>
		`).join('');
		$$('.sm-transcript-line', transcriptList).forEach(row => {
			const i = Number(row.dataset.lineIndex);
			$('[data-transcript-text]', row).addEventListener('input', (e) => { lines[i].text = e.target.value; });
			$('[data-transcript-split]', row).addEventListener('click', () => splitTranscriptLine(clip, i));
			const mergeBtn = $('[data-transcript-merge]', row);
			if (mergeBtn) mergeBtn.addEventListener('click', () => mergeTranscriptLineDown(clip, i));
		});
	}

	function splitTranscriptLine(clip, i) {
		const lines = clip.transcript;
		const line = lines[i];
		const words = line.text.split(/\s+/).filter(Boolean);
		if (words.length < 2) return; // nothing sensible to split
		const mid = Math.ceil(words.length / 2);
		const midSec = (timecodeToSeconds(line.start) + timecodeToSeconds(line.end)) / 2;
		lines.splice(i, 1,
			{ start: line.start, end: formatDuration(midSec), text: words.slice(0, mid).join(' ') },
			{ start: formatDuration(midSec), end: line.end, text: words.slice(mid).join(' ') }
		);
		renderTranscriptList(clip);
	}
	function mergeTranscriptLineDown(clip, i) {
		const lines = clip.transcript;
		if (i >= lines.length - 1) return;
		lines[i] = { start: lines[i].start, end: lines[i + 1].end, text: (lines[i].text + ' ' + lines[i + 1].text).trim() };
		lines.splice(i + 1, 1);
		renderTranscriptList(clip);
	}

	function effectiveSubtitleConfig(clip) {
		return { ...getSubtitlePreset(clip.subtitlePresetId), ...clip.subtitleOverrides };
	}

	function renderSubtitleEditorPreset(clip) {
		renderSubtitlePresetPicker(clipSubpresetGrid, state.accountPreset.subtitlePresets, clip.subtitlePresetId, 'sm-clip-subpreset', (id) => {
			clip.subtitlePresetId = id;
			clip.subtitleOverrides = {}; // switching preset resets per-clip overrides
			renderSubtitleEditorPreset(clip);
		});
		renderPresetControls(clipSubpresetControls, effectiveSubtitleConfig(clip), (field, value) => {
			clip.subtitleOverrides[field] = value;
		});
	}

	function openSubtitleEditor(clip) {
		subtitleEditorClipId = clip.id;
		subtitleDrawerTitle.textContent = `Субтитры · ${clip.title}`;
		subtitleRerenderStatus.textContent = clip.renderVersion > 1 ? `Версия v${clip.renderVersion}` : '';
		subtitleRerenderBtn.disabled = false;
		renderTranscriptList(clip);
		renderSubtitleEditorPreset(clip);
		subtitleDrawerOverlay.classList.remove('is-hidden');
	}
	function closeSubtitleEditor() {
		subtitleDrawerOverlay.classList.add('is-hidden');
		subtitleEditorClipId = null;
	}
	subtitleDrawerClose.addEventListener('click', closeSubtitleEditor);
	subtitleDrawerOverlay.addEventListener('click', (e) => { if (e.target === subtitleDrawerOverlay) closeSubtitleEditor(); });

	// Mock rerender: only this clip's card shows the cloth/"Пересборка…"
	// overlay while it runs — the rest of the grid, and the other clip
	// cards' own approve/skip state, are untouched.
	function requestSubtitleRerender(clip) {
		clip.rerendering = true;
		rerenderClip(clip);
		subtitleRerenderBtn.disabled = true;
		subtitleRerenderStatus.textContent = 'Пересобираем…';
		setTimeout(() => {
			clip.rerendering = false;
			clip.renderVersion++;
			rerenderClip(clip);
			subtitleRerenderBtn.disabled = false;
			subtitleRerenderStatus.textContent = `Готово · v${clip.renderVersion}`;
			showToast('Клип пересобран с новыми субтитрами', 'success');
		}, 1100 + Math.random() * 500);
	}
	subtitleRerenderBtn.addEventListener('click', () => {
		const clip = state.clips.items.find(c => c.id === subtitleEditorClipId);
		if (clip) requestSubtitleRerender(clip);
	});

	/* Initial paint */
	renderSourceStep();
	fetchTelegramStatus(); // real backend call — see the Telegram section above
	rehydrateFromUrl();    // real backend call — see the REAL BATCH FLOW section above

	/* ============================================================
	   NOT DONE YET (intentionally still frontend-mock — see the REAL
	   BATCH FLOW and Telegram sections above for everything that now
	   calls a real backend):
	   - single-clip retry after a compose failure (retryClip() shows an
	     honest "not supported yet" toast instead)
	   - subtitle quick-editor rerender, promo assets, auto-review scoring
	     (recommended is real but always false — see segments_from_analysis
	     in scripts/prod_job_store.py)
	   - clip.remoteId is real (set by applyBatchClips()); "Открыть в
	     редакторе"/OpenCut navigation itself still doesn't exist
	   - Telegram completion notifications (linking itself is real)
	   ============================================================ */
})();
