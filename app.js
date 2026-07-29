/**
 * SONYA Mini App - Main JavaScript
 * Telegram Mini App for Video Shorts Generation
 */

// =====================================================
// Telegram WebApp Integration
// =====================================================

let tg = null;


function isTelegramMiniAppLaunch() {
        const params = new URLSearchParams(window.location.search);
        const hash = window.location.hash || '';

        return Boolean(
                params.get('tgWebAppData') ||
                params.get('tgWebAppVersion') ||
                hash.includes('tgWebAppData=') ||
                hash.includes('tgWebAppVersion=')
        );
}

function loadTelegramSdk() {
        return new Promise((resolve, reject) => {
                if (window.Telegram?.WebApp) {
                        resolve(window.Telegram.WebApp);
                        return;
                }

                const script = document.createElement('script');
                script.src = 'https://telegram.org/js/telegram-web-app.js';
                script.async = true;

                script.onload = () => {
                        if (window.Telegram?.WebApp) {
                                resolve(window.Telegram.WebApp);
                        } else {
                                reject(new Error('Telegram WebApp SDK unavailable'));
                        }
                };

                script.onerror = () => {
                        reject(new Error('Telegram WebApp SDK failed to load'));
                };

                document.head.appendChild(script);
        });
}

async function initializeTelegramIfNeeded() {
        if (!isTelegramMiniAppLaunch()) {
                return;
        }

        try {
                tg = await loadTelegramSdk();

                if (!tg?.initData) {
                        tg = null;
                        return;
                }

                initTelegramApp();
        } catch (error) {
                console.error('Telegram initialization failed', error);
                tg = null;
        }
}

// User profile data
const userProfile = {
        id: null,
        name: 'User',
        username: '',
        avatar: '',
        subscription: 'free', // free, pro
        role: null // admin_developer, admin_ceo, or null
};

// Admin users
const ADMINS = {
        '1750740727': { role: 'DEVELOPER', label: 'Admin (Developer)' },
        '1027620514': { role: 'CEO', label: 'Admin (CEO)' }
};

// Subscription plans
// NOTE: single paid tier for now — SONYA Pro, 500 ₽ / 30 дней, без автопродления.
const SUBSCRIPTION_PLANS = {
        free: {
                name: 'Free Plan',
                icon: '⭐',
                limits: [
                        { label: 'Клипов в месяц', value: '2' },
                        { label: 'Длительность', value: 'до 30 сек' },
                        { label: 'Водяной знак', value: 'SONYA' }
                ]
        },
        pro: {
                name: 'SONYA Pro',
                icon: '⚡',
                price: 500,
                period: '30 дней',
                limits: [
                        { label: 'Клипов', value: 'без ограничений' },
                        { label: 'Длительность', value: 'без ограничений' },
                        { label: 'Субтитры + озвучка', value: '✓' },
                        { label: 'Приоритетная обработка', value: '✓' },
                        { label: 'Автопродление', value: 'нет' }
                ]
        }
};

// Initialize Telegram WebApp
function initTelegramApp() {
        if (tg) {
                tg.ready();
                tg.expand();

                // Apply Telegram theme colors if available
                document.documentElement.style.setProperty(
                        '--tg-theme-bg-color',
                        tg.themeParams.bg_color || '#0A0A0A'
                );

                // Set header color


                // Wait a bit for Telegram to fully initialize
                setTimeout(() => {
                        loadUserProfile();
                }, 100);
        } else {
                console.error('Telegram WebApp SDK not loaded!');
                // Try loading profile anyway with fallback
                loadUserProfile();
        }
}

// Load user profile from Telegram
function loadUserProfile() {
        
        // FIRST: Try to get user data from URL parameters (most reliable)
        const urlParams = new URLSearchParams(window.location.search);
        const urlUserId = urlParams.get('tg_user_id');
        
        if (urlUserId) {
                userProfile.id = urlUserId;
                userProfile.name = urlParams.get('tg_first_name') || 'User';
                
                const lastName = urlParams.get('tg_last_name');
                if (lastName) {
                        userProfile.name += ' ' + lastName;
                }
                
                const username = urlParams.get('tg_username');
                if (username) {
                        userProfile.username = '@' + username;
                }
                
                userProfile.avatar = generateAvatarUrl(userProfile.name);
                
                // Check if admin
                if (userProfile.id && ADMINS[userProfile.id]) {
                        userProfile.role = ADMINS[userProfile.id].role;
                        userProfile.subscription = 'pro';
                }
                
                updateProfileAvatar();
                return;
        }
        
        
        if (!tg) {
                
                // For testing outside Telegram
                userProfile.id = 'demo';
                userProfile.name = 'Demo User';
                userProfile.username = '@demo';
                userProfile.avatar = generateAvatarUrl('Demo User');
                updateProfileAvatar();
                return;
        }


        // Try multiple ways to get user data
        let user = null;
        
        // Method 1: initDataUnsafe.user (standard way)
        if (tg.initDataUnsafe && tg.initDataUnsafe.user) {
                user = tg.initDataUnsafe.user;
        }
        
        // Method 2: WebApp user data
        if (!user && tg.WebAppUser) {
                user = tg.WebAppUser;
        }

        // Method 3: Parse initData manually
        if (!user && tg.initData) {
                try {
                        const params = new URLSearchParams(tg.initData);
                        const userJson = params.get('user');
                        if (userJson) {
                                user = JSON.parse(userJson);
                        }
                } catch (e) {
                        console.error('Failed to parse initData:', e);
                }
        }

        // Method 4: Try window.TelegramWebviewProxy
        if (!user && window.TelegramWebviewProxy && window.TelegramWebviewProxy.postEvent) {
        }
        
        if (!user) {
                console.error('❌ FAILED: No Telegram user data available from any method');
                
                // Show visible warning to user
                userProfile.id = 'unknown';
                userProfile.name = '⚠️ Не удалось загрузить профиль';
                userProfile.username = 'Откройте через Telegram бота';
                userProfile.avatar = generateAvatarUrl('?');
                updateProfileAvatar();
                return;
        }

        
        // Ensure ID is string for comparison
        userProfile.id = user.id ? String(user.id) : null;
        userProfile.name = user.first_name || 'User';
        
        if (user.last_name) {
                userProfile.name += ' ' + user.last_name;
        }
        
        userProfile.username = user.username ? `@${user.username}` : '';
        
        // Try to get avatar from Telegram
        if (user.photo_url) {
                userProfile.avatar = user.photo_url;
        } else {
                // Fallback: generate avatar with initials
                userProfile.avatar = generateAvatarUrl(userProfile.name);
        }


        // Check if user is admin
        if (userProfile.id && ADMINS[userProfile.id]) {
                userProfile.role = ADMINS[userProfile.id].role;
                // Admin accounts get Pro-level access
                userProfile.subscription = 'pro';
        } else {
        }


        // Update avatar in UI
        updateProfileAvatar();
}

// Generate avatar URL from initials
function generateAvatarUrl(name) {
        const initials = name.split(' ').map(n => n[0]).join('').toUpperCase().substring(0, 2);
        // Красивые градиентные цвета для аватарок
        const colors = [
                'FF6B9D', // pink
                '4ECDC4', // teal
                '95E1D3', // mint
                'F38181', // coral
                'AA96DA', // purple
                'FCBAD3', // light pink
        ];
        // Выбираем цвет на основе первой буквы имени
        const colorIndex = name.charCodeAt(0) % colors.length;
        const bgColor = colors[colorIndex];
        
        return `https://ui-avatars.com/api/?name=${encodeURIComponent(initials)}&background=${bgColor}&color=fff&size=128&bold=true&font-size=0.5`;
}

// Update profile avatar in UI
function updateProfileAvatar() {
        const avatarElements = [
                document.getElementById('profile-avatar'),
                document.getElementById('profile-modal-avatar')
        ];

        avatarElements.forEach(el => {
                if (el) {
                        el.src = userProfile.avatar;
                        el.alt = userProfile.name;
                }
        });

        // If profile failed to load, add visual indicator
        if (userProfile.id === 'unknown') {
                const profileBtn = document.getElementById('profile-btn');
                if (profileBtn) {
                        profileBtn.style.borderColor = '#ff4444';
                        profileBtn.style.boxShadow = '0 0 10px rgba(255, 68, 68, 0.5)';
                }
        }
}

// =====================================================
// Theme Management
// =====================================================

function initTheme() {
        let savedTheme = localStorage.getItem('theme') || 'light';
        
        document.documentElement.setAttribute('data-theme', savedTheme);
        switchCinemaVideo(savedTheme);
        
        if (tg) {
                const themeColor = savedTheme === 'light' ? '#F5F5F7' : '#0A0A0A';


        }
}

function switchCinemaVideo(theme) {
        const video = document.getElementById('cinema-video');
        const source = document.getElementById('cinema-video-source');
        if (!video || !source) return;
        const src = theme === 'light'
                ? (video.dataset.lightSrc || 'bg-light.mp4')
                : (video.dataset.darkSrc  || "hf_20260419_161836_77c00607-b936-40e5-b41d-240635ddc9d9 (1).mp4");
        if (source.getAttribute('src') !== src) {
                source.setAttribute('src', src);
                video.load();
                video.play().catch(() => {});
        }
}

function toggleTheme() {
        const currentTheme = document.documentElement.getAttribute('data-theme');
        const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
        
        document.documentElement.setAttribute('data-theme', newTheme);
        localStorage.setItem('theme', newTheme);

        switchCinemaVideo(newTheme);
        
        if (tg) {
                const themeColor = newTheme === 'light' ? '#F5F5F7' : '#0A0A0A';


        }
}

// =====================================================
// State Management
// =====================================================

const appState = {
        currentPage: 'source',
        mode: 'url', // 'url' or 'upload'
        videoUrl: '',
        uploadedFile: null,
        brand: 'sonya', 
        clipType: '', // viral, educational, storytelling, hooks, filmbreaker
        settings: {
                subtitleStyle: 'viral', // viral, minimal, basic
                voiceoverEnabled: false,
                voiceType: 'male-1',
                translation: 'none'
        }
};

// =====================================================
// DOM Elements
// =====================================================

const elements = {
        // Top bar & menu
        mainTagline: document.getElementById('main-tagline'),
        menuToggle: document.getElementById('menu-toggle'),
        menuOverlay: document.getElementById('menu-overlay'),
        menuClose: document.getElementById('menu-close'),
        menuItems: document.querySelectorAll('.menu-item'),
        
        // Theme toggle
        themeToggle: document.getElementById('theme-toggle'),
        
        // Pages
        pages: {
                source: document.getElementById('page-source'),
                type: document.getElementById('page-type'),
                customize: document.getElementById('page-customize'),
                processing: document.getElementById('page-processing'),
                result: document.getElementById('page-result')
        },

        // Mode toggle
        modeBtns: document.querySelectorAll('.mode-btn'),
        modeUrl: document.getElementById('mode-url'),
        modeUpload: document.getElementById('mode-upload'),

        // URL input
        videoUrlInput: document.getElementById('video-url'),

        // File upload
        uploadZone: document.getElementById('upload-zone'),
        fileInput: document.getElementById('file-input'),
        fileInfo: document.getElementById('file-info'),
        fileName: document.getElementById('file-name'),
        removeFileBtn: document.getElementById('remove-file'),

        // Navigation buttons
        btnNext1: document.getElementById('btn-next-1'),
        btnNext2: document.getElementById('btn-next-2'),
        btnBack2: document.getElementById('btn-back-2'),
        btnBack3: document.getElementById('btn-back-3'),
        btnGenerate: document.getElementById('btn-generate'),
        btnDownload: document.getElementById('btn-download'),
        btnNewProject: document.getElementById('btn-new-project'),
        btnTrailer: document.getElementById('btn-trailer'),

        // Clip type
        clipTypeInputs: document.querySelectorAll('input[name="clip-type"]'),

        // Customization
        subtitlesToggle: document.getElementById('subtitles-toggle'),
        subtitleStyles: document.getElementById('subtitle-styles'),
        subtitleStyleInputs: document.querySelectorAll('input[name="subtitle-style"]'),
        voiceoverToggle: document.getElementById('voiceover-toggle'),
        voiceOptions: document.getElementById('voice-options'),
        voiceSelect: document.getElementById('voice-select'),
        languageInputs: document.querySelectorAll('input[name="language"]'),

        // Processing
        processingStatus: document.getElementById('processing-status'),
        progressBar: document.getElementById('progress-bar'),

        // Result
        resultCount: document.getElementById('result-count'),
        resultSize: document.getElementById('result-size')
};

// =====================================================
// Page Navigation
// =====================================================

function showPage(pageName) {
        // Hide all pages
        Object.values(elements.pages).forEach(page => {
                page.classList.remove('active');
        });

        // Show target page
        if (elements.pages[pageName]) {
                elements.pages[pageName].classList.add('active');
                appState.currentPage = pageName;
        }
}

// =====================================================
// Mode Toggle (URL / Upload)
// =====================================================

function setMode(mode) {
        appState.mode = mode;

        // Update buttons
        elements.modeBtns.forEach(btn => {
                btn.classList.toggle('active', btn.dataset.mode === mode);
        });

        // Show/hide input modes
        elements.modeUrl.classList.toggle('active', mode === 'url');
        elements.modeUpload.classList.toggle('active', mode === 'upload');

        // Update next button state
        updateNextButton1State();
}

// =====================================================
// URL Validation
// =====================================================

function isValidVideoUrl(url) {
        if (!url) return false;
        
        // Упрощенная проверка - просто проверяем, что это похоже на URL видео
        const patterns = [
                // YouTube
                /youtube\.com\/watch/i,
                /youtu\.be\//i,
                /youtube\.com\/embed/i,
                /youtube\.com\/v\//i,
                // Twitch
                /twitch\.tv\//i,
                // VK Video
                /vk\.com\/video/i,
                // Любой другой URL
                /^https?:\/\/.+/i
        ];

        return patterns.some(pattern => pattern.test(url));
}

function detectPlatform(url) {
        if (/youtube|youtu\.be/i.test(url)) return 'YouTube';
        if (/twitch\.tv/i.test(url)) return 'Twitch';
        if (/vk\.com/i.test(url)) return 'VK';
        return 'Unknown';
}

// =====================================================
// File Upload Handling
// =====================================================

function handleFileSelect(file) {
        if (!file) return;

        // Check if it's a video file
        if (!file.type.startsWith('video/')) {
                alert('Пожалуйста, выберите видео файл');
                return;
        }

        appState.uploadedFile = file;

        // Show file info
        elements.fileName.textContent = file.name;
        elements.fileInfo.classList.remove('hidden');
        elements.uploadZone.style.display = 'none';

        updateNextButton1State();
}

function removeFile() {
        appState.uploadedFile = null;
        elements.fileInput.value = '';
        elements.fileInfo.classList.add('hidden');
        elements.uploadZone.style.display = '';
        updateNextButton1State();
}

// =====================================================
// Button State Management
// =====================================================

function updateNextButton1State() {
        let isValid = false;

        if (appState.mode === 'url') {
                isValid = isValidVideoUrl(elements.videoUrlInput.value);
        } else {
                isValid = appState.uploadedFile !== null;
        }

        elements.btnNext1.disabled = !isValid;
}

function updateNextButton2State() {
        const selected = document.querySelector('input[name="clip-type"]:checked');
        const trailerSelected = appState.clipType === 'filmbreaker';
        elements.btnNext2.disabled = !selected && !trailerSelected;
}

// =====================================================
// Processing
// =====================================================
// NOTE: the actual progress UI is driven by the real backend-polling
// implementation at the bottom of this file (SONYA_REAL_POLLING_PATCH_V2),
// which overrides `simulateProcessing` as soon as this script loads.

function updateResultInfo() {
        // Simulated result data
        const videosCount = Math.floor(Math.random() * 8) + 5;
        const sizeInMB = Math.floor(Math.random() * 200) + 100;

        elements.resultCount.textContent = videosCount;
        elements.resultSize.textContent = sizeInMB + ' MB';
}

// =====================================================
// Data Collection
// =====================================================

function collectFormData() {
        // Если clipType уже установлен (например, для трейлера), использовать его
        let clipType = appState.clipType;
        if (!clipType) {
                clipType = document.querySelector('input[name="clip-type"]:checked')?.value;
        }

        return {
                source: {
                        mode: appState.mode,
                        url: appState.mode === 'url' ? elements.videoUrlInput.value : null,
                        fileName: appState.uploadedFile?.name || null,
                        platform: appState.mode === 'url' ? detectPlatform(elements.videoUrlInput.value) : 'upload'
                },
                brand: appState.brand || 'sonya',
                clipType: clipType || 'viral',
                settings: {
                        subtitleStyle: elements.subtitlesToggle?.checked 
                                ? (document.querySelector('input[name="subtitle-style"]:checked')?.value || 'viral')
                                : 'none',
                        voiceoverEnabled: elements.voiceoverToggle.checked,
                        voiceType: elements.voiceSelect.value,
                        translation: document.querySelector('input[name="language"]:checked')?.value
                }
        };
}

// =====================================================
// Telegram Bot Communication
// =====================================================

function sendDataToBot(data) {
        if (tg) {
                // Send data to the bot
                tg.sendData(JSON.stringify(data));
        } else {
                // Fallback for testing outside Telegram
                alert('Data would be sent to bot:\n' + JSON.stringify(data, null, 2));
        }
}

function requestZipDownload() {
        const data = {
                action: 'download_zip',
                ...collectFormData()
        };

        sendDataToBot(data);

        // Show confirmation
        if (tg) {
                tg.showAlert('Архив отправлен в чат!');
        }
}

// =====================================================
// Job Submission — single-flight guard
// =====================================================
// btnNext2 and btnGenerate are two separate UI entry points that both do
// the exact same thing: create a generation job and start polling. Without
// a shared, synchronous lock, a fast double-click (or click + Enter) on
// either button fires two independent async handlers, each calling
// checkAndCreateVideoJob() -> POST /api/generation/jobs, before the first
// one's await has a chance to resolve. submitGenerationJob() is the single
// function every trigger (click, Enter) must go through; the lock is set
// synchronously, before the first await, so a second call in the same
// event-loop tick is a guaranteed no-op.

let jobSubmitInFlight = false;

function setGenerateButtonsBusy(busy) {
        [elements.btnNext2, elements.btnGenerate].forEach(btn => {
                if (!btn) return;
                if (busy) {
                        if (btn.dataset.origText === undefined) {
                                btn.dataset.origText = btn.textContent;
                        }
                        btn.disabled = true;
                        btn.textContent = 'Создаём задачу…';
                } else {
                        btn.disabled = false;
                        if (btn.dataset.origText !== undefined) {
                                btn.textContent = btn.dataset.origText;
                                delete btn.dataset.origText;
                        }
                }
        });
}

// Called once the job reaches a terminal state (completed/failed/cancelled
// — see SONYA_REAL_POLLING_PATCH_V2 below) or the user starts a new
// project. Re-enables the buttons for the next submission.
function resetGenerationLock() {
        jobSubmitInFlight = false;
        setGenerateButtonsBusy(false);
}

async function submitGenerationJob() {
        if (jobSubmitInFlight) return; // synchronous check — no await above this line
        jobSubmitInFlight = true;
        setGenerateButtonsBusy(true);

        try {
                const formData = collectFormData();

                // checkAndCreateVideoJob is defined in auth.js (loaded after app.js)
                const result = (typeof checkAndCreateVideoJob === 'function')
                        ? await checkAndCreateVideoJob(formData)
                        : 'ok';

                if (result !== 'ok') {
                        // Auth/paywall modal opened, or a validation/network error —
                        // unlock so the user can retry.
                        resetGenerationLock();
                        return;
                }

                if (tg) sendDataToBot(formData);
                showPage('processing');
                simulateProcessing();
                // Lock stays held on success — released by resetGenerationLock()
                // once polling sees the job reach a terminal state, or by
                // btnNewProject's reset handler.
        } catch (err) {
                console.error('[SONYA] job submission failed', err);
                resetGenerationLock();
        }
}

// =====================================================
// Event Listeners
// =====================================================

function initEventListeners() {
        // Main menu (brand modes)
        if (elements.menuToggle && elements.menuOverlay) {
                const openMenu = () => {
                        elements.menuOverlay.classList.add('active');
                };
                const closeMenu = () => {
                        elements.menuOverlay.classList.remove('active');
                };

                elements.menuToggle.addEventListener('click', openMenu);
                
                if (elements.menuClose) {
                        elements.menuClose.addEventListener('click', closeMenu);
                }

                elements.menuOverlay.addEventListener('click', (event) => {
                        if (event.target === elements.menuOverlay) {
                                closeMenu();
                        }
                });

                elements.menuItems.forEach(item => {
                        item.addEventListener('click', () => {
                                const brand = item.getAttribute('data-brand-option');

                                appState.brand = brand;

                                // Обновить активное состояние кнопок
                                elements.menuItems.forEach(btn => {
                                        btn.classList.toggle('active', btn === item);
                                });

                                // Обновить текст под логотипом на главной
                                if (elements.mainTagline) {
                                        elements.mainTagline.textContent = 'Создавайте короткие видео вместе с нами';
                                }

                                closeMenu();
                        });
                });
        }
        
        // Theme toggle
        if (elements.themeToggle) {
                elements.themeToggle.addEventListener('click', toggleTheme);
        } else {
                console.error('❌ Кнопка переключения темы не найдена!');
        }
        
        // Profile modal
        const profileBtn = document.getElementById('profile-btn');
        const profileModalOverlay = document.getElementById('profile-modal-overlay');
        const profileModalClose = document.getElementById('profile-modal-close');

        if (profileBtn) {
                profileBtn.addEventListener('click', () => {
                        openProfileModal();
                });
        }

        if (profileModalClose) {
                profileModalClose.addEventListener('click', () => {
                        closeProfileModal();
                });
        }

        if (profileModalOverlay) {
                profileModalOverlay.addEventListener('click', (e) => {
                        if (e.target === profileModalOverlay) {
                                closeProfileModal();
                        }
                });
        }

        // "Тарифы" info button in the profile modal — opens the real
        // (backend-connected) paywall modal instead of the old fake
        // 3-tier plans modal. `openPaywallModal` is defined in auth.js,
        // loaded after app.js.
        const subscriptionInfoBtn = document.getElementById('subscription-info-btn');
        if (subscriptionInfoBtn) {
                subscriptionInfoBtn.addEventListener('click', (e) => {
                        e.stopPropagation();
                        if (typeof openPaywallModal === 'function') openPaywallModal();
                });
        }

        function closeProfileModal() {
                if (profileModalOverlay) {
                        profileModalOverlay.classList.remove('active');
                }
        }

        function updateProfileModal() {
                // Update name
                const nameEl = document.getElementById('profile-name');
                if (nameEl) nameEl.textContent = userProfile.name;

                // Update username
                const usernameEl = document.getElementById('profile-username');
                if (usernameEl) usernameEl.textContent = userProfile.username || '';

                // Update ID
                const idEl = document.getElementById('profile-id');
                if (idEl) idEl.textContent = userProfile.id || '—';

                // Update status badge
                const plan = SUBSCRIPTION_PLANS[userProfile.subscription];
                const statusIcon = document.querySelector('.status-icon');
                const statusText = document.getElementById('profile-status-text');
                
                if (statusIcon && plan) statusIcon.textContent = plan.icon;
                if (statusText) statusText.textContent = userProfile.subscription.toUpperCase();

                // Update role
                const roleEl = document.getElementById('profile-role');
                if (roleEl) {
                        if (userProfile.role) {
                                const adminInfo = ADMINS[userProfile.id];
                                roleEl.textContent = adminInfo ? adminInfo.label : '';
                                roleEl.style.display = 'block';
                        } else {
                                roleEl.style.display = 'none';
                        }
                }

                // Update subscription info
                const planNameEl = document.getElementById('subscription-plan-name');
                if (planNameEl && plan) {
                        planNameEl.textContent = plan.name;
                }

                const limitsEl = document.getElementById('subscription-limits');
                if (limitsEl && plan && plan.limits) {
                        limitsEl.innerHTML = plan.limits.map(limit => `
                                <div class="limit-item">
                                        <span class="limit-label">${limit.label}:</span>
                                        <span class="limit-value">${limit.value}</span>
                                </div>
                        `).join('');
                }
        }

      
        // Mode toggle
        elements.modeBtns.forEach(btn => {
                btn.addEventListener('click', () => setMode(btn.dataset.mode));
        });

        // URL input
        elements.videoUrlInput.addEventListener('input', updateNextButton1State);

        // File upload - click handled natively by <label for="file-input">
        // No extra JS click needed — adding it caused double-open
        elements.fileInput.addEventListener('change', (e) => {
                handleFileSelect(e.target.files[0]);
                updateNextButton1State();
        });

        // File upload - drag and drop
        elements.uploadZone.addEventListener('dragover', (e) => {
                e.preventDefault();
                elements.uploadZone.classList.add('dragover');
        });

        elements.uploadZone.addEventListener('dragleave', () => {
                elements.uploadZone.classList.remove('dragover');
        });

        elements.uploadZone.addEventListener('drop', (e) => {
                e.preventDefault();
                elements.uploadZone.classList.remove('dragover');
                handleFileSelect(e.dataTransfer.files[0]);
        });

        // Remove file
        elements.removeFileBtn.addEventListener('click', () => {
                removeFile();
                updateNextButton1State();
        });

        // Navigation - Page 1 to 2
        elements.btnNext1.addEventListener('click', () => {
                appState.videoUrl = elements.videoUrlInput.value;
                showPage('type');
        });

        // Trailer button
        elements.btnTrailer.addEventListener('click', () => {
                // Снять все выборы типов клипов
                elements.clipTypeInputs.forEach(input => {
                        input.checked = false;
                });
                
                // Установить тип трейлера
                appState.clipType = 'filmbreaker';
                
                // Визуально отметить кнопку как выбранную
                elements.btnTrailer.classList.add('selected');
                
                // Обновить состояние кнопки "Продолжить"
                updateNextButton2State();
        });

        // Navigation - Page 2 → Processing
        // New flow: check auth + POST /api/videos/create before starting.
        // Routed through submitGenerationJob() — the single-flight guard —
        // so a double-click or a click racing an Enter press never fires a
        // second POST /api/generation/jobs.
        elements.btnNext2.addEventListener('click', submitGenerationJob);
        elements.btnNext2.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') submitGenerationJob();
        });

        // Navigation - Back buttons
        elements.btnBack2.addEventListener('click', () => showPage('source'));
        elements.btnBack3.addEventListener('click', () => showPage('type'));

        // Clip type selection
        elements.clipTypeInputs.forEach(input => {
                input.addEventListener('change', () => {
                        // При выборе типа клипа снимаем выбор трейлера
                        appState.clipType = '';
                        if (elements.btnTrailer) {
                                elements.btnTrailer.classList.remove('selected');
                        }
                        updateNextButton2State();
                });
        });

        // Subtitles toggle
        elements.subtitlesToggle.addEventListener('change', () => {
                elements.subtitleStyles.classList.toggle('hidden', !elements.subtitlesToggle.checked);
        });

        // Voiceover toggle
        elements.voiceoverToggle.addEventListener('change', () => {
                elements.voiceOptions.classList.toggle('hidden', !elements.voiceoverToggle.checked);
        });

        // Generate button (page 3 / editor flow)
        // Same submitGenerationJob() single-flight guard as btnNext2 — both
        // buttons lead to the exact same job-creation call.
        elements.btnGenerate.addEventListener('click', submitGenerationJob);
        elements.btnGenerate.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') submitGenerationJob();
        });

        // Download ZIP button
        elements.btnDownload.addEventListener('click', requestZipDownload);

        // New project button
        elements.btnNewProject.addEventListener('click', () => {
                // Reset state
                resetGenerationLock();
                // clearJobIdempotencyKey is defined in auth.js (loaded after app.js)
                if (typeof clearJobIdempotencyKey === 'function') clearJobIdempotencyKey();
                elements.videoUrlInput.value = '';
                appState.uploadedFile = null;
                removeFile();

                // Reset radio buttons
                elements.clipTypeInputs.forEach(input => input.checked = false);
                elements.subtitleStyleInputs.forEach(input => {
                        input.checked = input.value === 'viral';
                });
                elements.languageInputs.forEach(input => {
                        input.checked = input.value === 'none';
                });

                // Reset customization
                elements.subtitlesToggle.checked = false;
                elements.subtitleStyles.classList.add('hidden');
                elements.voiceoverToggle.checked = false;
                elements.voiceOptions.classList.add('hidden');

                // Reset clip type
                appState.clipType = '';

                // Reset buttons
                updateNextButton1State();
                updateNextButton2State();

                // Go to first page
                showPage('source');
        });
}

// =====================================================
// Initialization
// =====================================================

function init() {
        initTheme();
        if (
                window.Telegram &&
                window.Telegram.WebApp &&
                typeof window.Telegram.WebApp.initData === 'string' &&
                window.Telegram.WebApp.initData.length > 0
        ) {
                initializeTelegramIfNeeded();
        }
        initEventListeners();

        appState.brand = 'sonya';

        if (elements.mainTagline) {
                elements.mainTagline.textContent = 'Создавайте короткие видео вместе с нами';
        }

        elements.menuItems.forEach(item => {
                item.classList.toggle(
                        'active',
                        item.getAttribute('data-brand-option') === 'sonya'
                );
        });

        if (elements.subtitlesToggle) {
                elements.subtitlesToggle.checked = false;
                elements.subtitleStyles.classList.add('hidden');
        }
        if (elements.voiceoverToggle) {
                elements.voiceoverToggle.checked = false;
                elements.voiceOptions.classList.add('hidden');
        }
        
        // При возврате на страницу 2 сбрасываем выбор трейлера если выбран тип клипа
        // Это обрабатывается в обработчике изменения типа клипа

        initLiteEditor();

        // "Доработать в редакторе" on result page → open OpenCut as a separate page
        const btnOpenEditor = document.getElementById('btn-open-editor');
        if (btnOpenEditor) {
                btnOpenEditor.addEventListener('click', () => {
                        window.location.href = 'opencut.html';
                });
        }

}

// =====================================================
// Lite Editor (Page 3) — purely visual companion
// · attaches uploaded file / URL as preview source
// · wires play/pause + aspect ratio switcher
// · never touches AI state or form submission
// =====================================================
function initLiteEditor() {
        const preview    = document.getElementById('editor-preview');
        const playBtn    = document.getElementById('editor-play');
        const playIcon   = playBtn ? playBtn.querySelector('i') : null;
        const playTool   = document.querySelector('.editor-tool[data-action="play"]');
        const stageInner = document.querySelector('.editor-stage-inner');
        const aspectBtns = document.querySelectorAll('.editor-aspect-btn');
        const timeEl     = document.getElementById('editor-time');

        if (!preview || !stageInner) return;

        let currentObjectURL = null;
        const setSource = (src) => {
                if (!src) return;
                if (preview.src === src) return;
                if (currentObjectURL) {
                        try { URL.revokeObjectURL(currentObjectURL); } catch {}
                        currentObjectURL = null;
                }
                preview.src = src;
                preview.load();
        };

        const syncPreview = () => {
                if (appState.uploadedFile) {
                        currentObjectURL = URL.createObjectURL(appState.uploadedFile);
                        setSource(currentObjectURL);
                } else {
                        const cinema = document.querySelector('#cinema-video source');
                        if (cinema && cinema.src) setSource(cinema.src);
                }
        };

        // Observe when page-customize becomes active to (re)sync preview
        const page = document.getElementById('page-customize');
        if (page) {
                const observer = new MutationObserver(() => {
                        if (page.classList.contains('active')) syncPreview();
                });
                observer.observe(page, { attributes: true, attributeFilter: ['class'] });
        }

        // Play / pause
        const togglePlay = () => {
                if (preview.paused) { preview.play().catch(() => {}); }
                else { preview.pause(); }
        };
        if (playBtn)  playBtn.addEventListener('click', togglePlay);
        if (playTool) playTool.addEventListener('click', togglePlay);

        const reflectPlayState = () => {
                const paused = preview.paused;
                if (playIcon) {
                        playIcon.classList.toggle('fa-play',  paused);
                        playIcon.classList.toggle('fa-pause', !paused);
                }
                if (playTool) {
                        const i = playTool.querySelector('i');
                        if (i) {
                                i.classList.toggle('fa-play',  paused);
                                i.classList.toggle('fa-pause', !paused);
                        }
                }
        };
        preview.addEventListener('play',  reflectPlayState);
        preview.addEventListener('pause', reflectPlayState);

        // Time label
        const fmt = (sec) => {
                if (!isFinite(sec)) return '00:00';
                const m = Math.floor(sec / 60).toString().padStart(2, '0');
                const s = Math.floor(sec % 60).toString().padStart(2, '0');
                return `${m}:${s}`;
        };
        preview.addEventListener('timeupdate', () => {
                if (timeEl) timeEl.textContent = `${fmt(preview.currentTime)} / ${fmt(preview.duration)}`;
        });
        preview.addEventListener('loadedmetadata', () => {
                if (timeEl) timeEl.textContent = `00:00 / ${fmt(preview.duration)}`;
        });

        // Aspect ratio switcher (visual only)
        aspectBtns.forEach(btn => {
                btn.addEventListener('click', () => {
                        aspectBtns.forEach(b => b.classList.remove('is-active'));
                        btn.classList.add('is-active');
                        const ratio = btn.dataset.ratio || '16:9';
                        stageInner.style.aspectRatio = ratio.replace(':', ' / ');
                });
        });
}

// Start the app
document.addEventListener('DOMContentLoaded', init);


/* SONYA_REAL_POLLING_PATCH_V2 */
(function () {
  const API_BASE = (window.SONYA_API_BASE || '/api').replace(/\/$/, '');

  function pickJobId(job) {
    return job && (job.job_id || job.id);
  }

  function activeJobId() {
    return pickJobId(window.SONYA_LAST_JOB) || localStorage.getItem('sonya_active_job_id');
  }

  function statusText(job) {
    const st = String(job && job.status || '').toLowerCase();
    if (st === 'queued') return 'Видео в очереди';
    if (st === 'claimed') return 'GPU забрал задачу';
    if (st === 'processing') return 'Генерация видео';
    if (st === 'mode_running') return 'AI анализирует и монтирует';
    if (st === 'completed') return 'Видео готово';
    if (st === 'failed') return 'Ошибка генерации';
    return st ? ('Статус: ' + st) : 'Подготовка задачи';
  }

  function setText(text) {
    const selectors = [
      '#processing-status',
      '#processing-title',
      '.processing-status',
      '.processing-title',
      '.status-text',
      '.progress-text',
      '[data-processing-status]'
    ];

    selectors.forEach(sel => {
      document.querySelectorAll(sel).forEach(el => {
        el.textContent = text;
      });
    });

  }

  async function getJson(url) {
    const res = await fetch(url, { credentials: 'include' });
    let data = {};
    try { data = await res.json(); } catch (_) {}
    return { res, data };
  }

  // Backend-controlled URL (S3 presigned result link) must never be trusted
  // as-is: only an absolute https: URL, or a same-origin URL (any scheme
  // the page itself is already served over), is allowed. Rejects
  // javascript:, data:, vbscript:, and any malformed value -- those get an
  // opaque/null URL.origin from the URL constructor, which can never equal
  // window.location.origin, so both branches below fail closed.
  function isSafeResultUrl(rawUrl) {
    if (typeof rawUrl !== 'string' || !rawUrl.trim()) return false;
    let parsed;
    try {
      parsed = new URL(rawUrl, window.location.href);
    } catch (_) {
      return false;
    }
    if (parsed.protocol === 'https:') return true;
    if (parsed.origin === window.location.origin) return true;
    return false;
  }

  function showRealResult(result, job) {
    localStorage.removeItem('sonya_active_job_id');
    if (typeof resetGenerationLock === 'function') resetGenerationLock();

    const rawUrl =
      result.url ||
      result.result_url ||
      result.download_url ||
      result.presigned_url ||
      result.signed_url ||
      '';
    const url = isSafeResultUrl(rawUrl) ? rawUrl : '';

    window.SONYA_LAST_RESULT_URL = url;
    window.SONYA_LAST_COMPLETED_JOB = job;

    if (typeof showPage === 'function') {
      showPage('result');
    }

    let box = document.getElementById('sonya-real-result');
    if (!box) {
      box = document.createElement('div');
      box.id = 'sonya-real-result';
      box.style.cssText = 'margin:24px auto;max-width:760px;padding:20px;border:1px solid rgba(255,255,255,.14);border-radius:20px;background:rgba(255,255,255,.05);color:#fff;text-align:center;position:relative;z-index:20;';
      document.body.appendChild(box);
    }

    // Rebuild contents via DOM APIs only -- never innerHTML with anything
    // derived from the backend response. url has already been validated by
    // isSafeResultUrl(); .src/.href are plain property assignments (never
    // parsed as markup), not string concatenation into an HTML template.
    while (box.firstChild) box.removeChild(box.firstChild);

    if (url) {
      const title = document.createElement('div');
      title.style.cssText = 'font-size:22px;margin-bottom:14px;';
      title.textContent = 'Видео готово';

      const video = document.createElement('video');
      video.controls = true;
      video.style.cssText = 'width:100%;max-height:520px;border-radius:16px;background:#000;';
      video.src = url;

      const linkWrap = document.createElement('div');
      linkWrap.style.cssText = 'margin-top:16px;';
      const link = document.createElement('a');
      link.href = url;
      link.target = '_blank';
      link.rel = 'noopener';
      link.style.cssText = 'color:#fff;text-decoration:underline;font-size:18px;';
      link.textContent = 'Скачать видео';
      linkWrap.appendChild(link);

      box.appendChild(title);
      box.appendChild(video);
      box.appendChild(linkWrap);
    } else {
      const msg = document.createElement('div');
      msg.style.cssText = 'font-size:20px;';
      msg.textContent = rawUrl
        ? 'Видео готово, но получена небезопасная ссылка на результат. Обратитесь в поддержку. Job: ' + (pickJobId(job) || '')
        : 'Видео готово, но ссылка результата не найдена. Job: ' + (pickJobId(job) || '');
      box.appendChild(msg);
    }
  }

  // Reentrancy guard: btnNext2/btnGenerate (via simulateProcessing) AND the
  // DOMContentLoaded resume-from-localStorage check below can both try to
  // start a poll for the same (or a different) job. Never run two polling
  // loops concurrently — the second call is a no-op while one is active.
  let pollInFlight = false;

  async function pollJob(jobId) {
    if (!jobId) {
      setText('Не найден job_id. Создайте задачу заново.');
      return;
    }

    if (pollInFlight) return;
    pollInFlight = true;

    localStorage.setItem('sonya_active_job_id', jobId);
    setText('Видео в очереди');

    try {
      for (let i = 0; i < 720; i++) {
        const { res, data: job } = await getJson(API_BASE + '/generation/jobs/' + jobId);

        if (!res.ok) {
          setText('Ждём статус задачи...');
          await new Promise(r => setTimeout(r, 5000));
          continue;
        }

        const st = String(job.status || '').toLowerCase();
        setText(statusText(job));

        if (st === 'completed') {
          const result = await getJson(API_BASE + '/generation/jobs/' + jobId + '/result-url');
          showRealResult(result.data || {}, job);
          return;
        }

        if (st === 'failed' || st === 'cancelled') {
          localStorage.removeItem('sonya_active_job_id');
          if (typeof resetGenerationLock === 'function') resetGenerationLock();
          setText(job.error || job.last_error || 'Ошибка генерации');
          return;
        }

        await new Promise(r => setTimeout(r, 5000));
      }
    } finally {
      pollInFlight = false;
    }

    setText('Задача ещё выполняется. Обновите страницу позже.');
  }

  window.sonyaPollJob = pollJob;

  try {
    window.simulateProcessing = function () {
      pollJob(activeJobId());
    };
    simulateProcessing = window.simulateProcessing;
  } catch (e) {
  }

  document.addEventListener('DOMContentLoaded', function () {
    const jobId = localStorage.getItem('sonya_active_job_id');
    if (jobId && location.pathname.includes('processing')) {
      pollJob(jobId);
    }
  });
})();
