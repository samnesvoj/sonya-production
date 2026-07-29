/**
 * SONYA Auth layer
 * – API client (credentials:include for HttpOnly cookies)
 * – Auth state (refreshed from /api/auth/me, never from localStorage)
 * – Account button + dropdown
 * – Auth modal (login / registration with 3 consents) — Glass UI
 * – Paywall modal (FREE_PLAN_USED stub)
 * – Legal placeholder modals
 */

/* ─────────────────────────────────────────────
   CONFIG
───────────────────────────────────────────── */
// In production point this to your API domain.
// Empty string = same origin (works when backend serves the frontend too).
const SONYA_API_BASE = window.SONYA_API_BASE || '';

/* ─────────────────────────────────────────────
   AUTH STATE  (single source of truth)
   Never write subscription / limits here from
   frontend code — always refresh from /api/me.
───────────────────────────────────────────── */
const authState = {
  initialized: false,
  user: null,       // null = not logged in
  // user = { user_id, email, plan_type, plan_status, plan_active_until,
  //          free_video_limit, free_video_used, telegram_linked }
};

/* ─────────────────────────────────────────────
   API CLIENT
───────────────────────────────────────────── */
async function apiFetch(path, options = {}) {
  const url = SONYA_API_BASE + path;
  const defaults = {
    credentials: 'include',  // send HttpOnly session cookie
    headers: { 'Content-Type': 'application/json' },
  };
  try {
    const headers = { ...defaults.headers, ...(options.headers || {}) };
    if (typeof FormData !== 'undefined' && options.body instanceof FormData) {
      delete headers['Content-Type'];
    }
    const res = await fetch(url, { ...defaults, ...options, headers });
    return res;
  } catch (e) {
    // Network / CORS failure
    console.error('[SONYA API] Network error:', e);
    return { ok: false, status: 0, _networkError: true,
      json: async () => ({ detail: 'Сервер недоступен. Проверьте соединение.' }) };
  }
}

async function apiGetMe() {
  return apiFetch('/auth/me');
}


function normalizeApiAuthPurpose(purpose) {
  return purpose === 'registration' ? 'register' : purpose;
}

function normalizeApiConsents(consents) {
  if (!consents) {
    return {};
  }
  if (Array.isArray(consents)) {
    return {
      terms: consents.includes('terms'),
      privacy: consents.includes('privacy'),
      personal_data: consents.includes('personal_data') || consents.includes('personalData'),
    };
  }
  return consents;
}

async function apiRequestCode(email, purpose) {
  return apiFetch('/auth/request-code', {
    method: 'POST',
    body: JSON.stringify({ email, purpose: normalizeApiAuthPurpose(purpose) }),
  });
}

async function apiVerifyCode(payload) {
  // payload: { email, code, purpose, consents? }
  return apiFetch('/auth/verify-code', {
    method: 'POST',
    body: JSON.stringify({ ...payload, purpose: normalizeApiAuthPurpose(payload.purpose), consents: normalizeApiConsents(payload.consents) }),
  });
}

async function apiLogout() {
  return apiFetch('/auth/logout', { method: 'POST' });
}


function normalizeMode(clipType) {
  const v = String(clipType || '').toLowerCase();

  if (['filmbreaker', 'trailer', 'cinematic', 'cinematic_trailer', 'cinematic trailer'].includes(v)) {
    return 'trailer_film_breaker';
  }
  if (['viral', 'virality'].includes(v)) return 'virality';
  if (['storytelling', 'stories'].includes(v)) return 'stories';
  if (['educational', 'education'].includes(v)) return 'educational';
  if (['streamer', 'stream'].includes(v)) return 'streamer';
  if (['sonya_gen', 'gen', 'gen-1'].includes(v)) return 'sonya_gen';
  if (v === 'hooks') return 'virality';

  return 'trailer_film_breaker';
}

async function apiCreateVideoJob(formData) {
  const sourceUrl = String(formData?.source?.url || '').trim();

  // appState is defined in app.js, loaded before auth.js.
  const uploadedFile =
    formData?.file ||
    formData?.source?.file ||
    (typeof appState !== 'undefined' ? appState.uploadedFile : null) ||
    (typeof window !== 'undefined' && window.appState ? window.appState.uploadedFile : null);

  if (!uploadedFile) {
    return {
      ok: false,
      status: 400,
      json: async () => ({
        detail: {
          error: 'file_required',
          message: sourceUrl
            ? 'Загрузка по ссылке YouTube/Twitch будет подключена следующим этапом. Сейчас загрузите видеофайл.'
            : 'Выберите видеофайл для генерации.'
        }
      })
    };
  }

  const mode = normalizeMode(formData?.clipType || formData?.mode);
  const fd = new FormData();
  fd.append('mode', mode);
  fd.append('file', uploadedFile, uploadedFile.name || 'input.mp4');
  fd.append('params', JSON.stringify({
    ...formData,
    source: {
      ...(formData?.source || {}),
      url: sourceUrl,
      fileName: uploadedFile.name || formData?.source?.fileName || null,
      platform: 'upload'
    },
    frontend: 'legacy_static_sonya',
    production_endpoint: '/api/generation/jobs'
  }));

  return apiFetch('/generation/jobs', {
    method: 'POST',
    body: fd,
    headers: {}
  });
}

async function apiGetSubscriptionStatus() {
  return apiFetch('/billing/subscription-status');
}

/* ─────────────────────────────────────────────
   REFRESH AUTH STATE  (call after login/logout)
───────────────────────────────────────────── */
async function refreshAuthState() {
  const res = await apiGetMe();
  if (res.status === 401 || !res.ok) {
    authState.user = null;
  } else {
    try {
      authState.user = await res.json();
    } catch {
      authState.user = null;
    }
  }
  authState.initialized = true;
  renderAccountButton();
  return authState.user;
}

/* ─────────────────────────────────────────────
   ACCOUNT BUTTON RENDERING
───────────────────────────────────────────── */
function renderAccountButton() {
  const btn = document.getElementById('sonya-account-btn');
  if (!btn) return;

  const user = authState.user;
  if (!user) {
    btn.innerHTML = '<i class="fa-solid fa-user"></i>';
    btn.setAttribute('aria-label', 'Аккаунт');
    btn.classList.remove('is-logged-in');
  } else {
    // Show first letter of email as avatar
    const initials = user.email ? user.email[0].toUpperCase() : '?';
    btn.innerHTML = `<span class="acct-initials">${initials}</span>`;
    btn.setAttribute('aria-label', user.email);
    btn.classList.add('is-logged-in');
  }
}

/* ─────────────────────────────────────────────
   ACCOUNT DROPDOWN
───────────────────────────────────────────── */
function openAccountDropdown() {
  const drop = document.getElementById('sonya-account-drop');
  if (!drop) return;

  const user = authState.user;
  if (!user) {
    drop.innerHTML = `
      <button class="acct-drop-item" id="acct-drop-login">
        <i class="fa-solid fa-arrow-right-to-bracket"></i> Войти
      </button>
      <button class="acct-drop-item" id="acct-drop-register">
        <i class="fa-solid fa-user-plus"></i> Зарегистрироваться
      </button>`;
    drop.querySelector('#acct-drop-login').onclick = () => { closeAccountDropdown(); openAuthModal('login'); };
    drop.querySelector('#acct-drop-register').onclick = () => { closeAccountDropdown(); openAuthModal('registration'); };
  } else {
    const emailLower = String(user.email || '').toLowerCase();
    const planLabel =
      emailLower === 'elcuevimran@gmail.com' && user.plan_type === 'admin' ? 'CEO' :
      emailLower === 'mironowism@gmail.com' && user.plan_type === 'admin' ? 'Admin' :
      user.plan_type === 'admin' ? 'Admin' :
      user.plan_type === 'pro' ? 'Pro Plan ✦' :
      'Free Plan';
    const freeLeft = user.free_video_limit - user.free_video_used;
    const until = user.plan_active_until
      ? new Date(user.plan_active_until).toLocaleDateString('ru-RU')
      : null;

    drop.innerHTML = `
      <div class="acct-drop-user">
        <span class="acct-drop-email">${escHtml(user.email)}</span>
        <span class="acct-drop-plan ${user.plan_type === 'pro' || user.plan_type === 'admin' ? 'is-pro' : ''}">${planLabel}</span>
        ${until ? `<span class="acct-drop-until">до ${until}</span>` : ''}
        ${user.plan_type === 'free'
          ? `<span class="acct-drop-free">Бесплатных видео: ${freeLeft} / ${user.free_video_limit}</span>`
          : ''}
      </div>
      <div class="acct-drop-divider"></div>
      ${user.plan_type === 'free'
        ? `<button class="acct-drop-item acct-drop-upgrade" id="acct-drop-upgrade">
             <i class="fa-solid fa-bolt"></i> Перейти на Pro
           </button>`
        : ''}
      <button class="acct-drop-item acct-drop-logout" id="acct-drop-logout">
        <i class="fa-solid fa-right-from-bracket"></i> Выйти
      </button>`;

    if (user.plan_type === 'free') {
      drop.querySelector('#acct-drop-upgrade').onclick = () => { closeAccountDropdown(); openPaywallModal(); };
    }
    drop.querySelector('#acct-drop-logout').onclick = handleLogout;
  }

  drop.classList.add('is-open');

  // Close on outside click
  setTimeout(() => {
    document.addEventListener('click', onOutsideDropdown, { once: true });
  }, 0);
}

function closeAccountDropdown() {
  const drop = document.getElementById('sonya-account-drop');
  if (drop) drop.classList.remove('is-open');
}

function onOutsideDropdown(e) {
  const drop = document.getElementById('sonya-account-drop');
  const btn  = document.getElementById('sonya-account-btn');
  if (drop && !drop.contains(e.target) && e.target !== btn) {
    closeAccountDropdown();
  }
}

async function handleLogout() {
  closeAccountDropdown();
  await apiLogout();
  await refreshAuthState();
}

/* ─────────────────────────────────────────────
   AUTH MODAL  (Glass UI, login / registration / code)
───────────────────────────────────────────── */
let _authPurpose  = 'login';   // 'login' | 'registration'
let _authView     = 'login';   // 'login' | 'register' | 'code'
let _authEmail    = '';        // remembered email used to request code
let _authConsents = null;      // remembered consents array (registration only)

/* Resend code cooldown (UI only — backend has its own throttling) */
const RESEND_COOLDOWN_SECONDS = 45;
let _resendDeadline = 0;
let _resendTimerId  = null;

function startResendCooldown() {
  _resendDeadline = Date.now() + RESEND_COOLDOWN_SECONDS * 1000;
  refreshResendButton();
  if (_resendTimerId) clearInterval(_resendTimerId);
  _resendTimerId = setInterval(refreshResendButton, 1000);
}
function stopResendCooldown() {
  if (_resendTimerId) { clearInterval(_resendTimerId); _resendTimerId = null; }
}
function refreshResendButton() {
  const btn = document.getElementById('sonya-auth-resend');
  if (!btn) return;
  const remaining = Math.max(0, Math.ceil((_resendDeadline - Date.now()) / 1000));
  if (remaining > 0) {
    btn.disabled = true;
    btn.textContent = `Отправить код повторно (${remaining}s)`;
  } else {
    btn.disabled = false;
    btn.textContent = 'Отправить код повторно';
    stopResendCooldown();
  }
}

/* ── helpers: CTA text + loading (preserve SVG icons) ── */
function setCtaText(btn, text) {
  if (!btn) return;
  const span = btn.querySelector('span');
  if (span) span.textContent = text;
  else      btn.textContent  = text;
}

function setCtaLoading(btn, loading) {
  if (!btn) return;
  btn.disabled = !!loading;
  btn.classList.toggle('is-loading', !!loading);
}

/* ── open / close ── */
function openAuthModal(mode = 'login', message = '') {
  const overlay = document.getElementById('sonya-auth-overlay');
  if (!overlay) return;

  // Normalize mode: callers may use 'registration' (legacy) or 'register'.
  const view = (mode === 'registration' || mode === 'register') ? 'register' : 'login';
  setAuthView(view);
  setAuthBanner(message);
  clearAuthError();

  overlay.inert = false;
  overlay.setAttribute('aria-hidden', 'false');
  overlay.classList.add('is-open');
  document.body.classList.add('sonya-auth-open');
  document.body.style.overflow = 'hidden';

  setTimeout(() => {
    const id = view === 'register' ? 'sonya-register-email' : 'sonya-login-email';
    document.getElementById(id)?.focus();
  }, 80);
}

function closeAuthModal() {
  const overlay = document.getElementById('sonya-auth-overlay');
  if (!overlay) return;
  overlay.classList.remove('is-open');
  const focused = document.activeElement;
  if (focused && overlay.contains(focused)) {
    focused.blur();
  }

  overlay.inert = true;
  overlay.setAttribute('aria-hidden', 'true');
  document.body.classList.remove('sonya-auth-open');
  document.body.style.overflow = '';
  clearAuthError();
  setAuthBanner('');
  stopResendCooldown();
}

/* ── view switching ── */
function setAuthView(view) {
  _authView = view;

  // Tabs reflect login/register; in 'code' view neither is active.
  document.querySelectorAll('[data-auth-tab]').forEach(tab => {
    tab.classList.toggle('is-active',
      (view === 'login'    && tab.dataset.authTab === 'login') ||
      (view === 'register' && tab.dataset.authTab === 'register'));
  });

  document.querySelectorAll('[data-auth-view]').forEach(v => {
    v.classList.toggle('is-active', v.dataset.authView === view);
  });
}

/* ── banner / error ── */
function setAuthBanner(msg) {
  const el = document.getElementById('sonya-auth-banner');
  if (!el) return;
  if (msg) { el.textContent = msg; el.hidden = false; }
  else     { el.textContent = ''; el.hidden = true; }
}

function normalizeAuthError(value, fallback = 'Не удалось выполнить запрос') {
  if (typeof value === 'string' && value.trim()) {
    return value;
  }

  if (value && typeof value === 'object') {
    const code = value.error || value.code || '';

    const messages = {
      unauthorized: 'Необходимо войти в аккаунт',
      invalid_email: 'Введите корректный email',
      email_send_failed: 'Не удалось отправить письмо. Попробуйте позже.',
      rate_limited: 'Слишком много попыток. Подождите и повторите.',
      invalid_code: 'Неверный код. Проверьте письмо и повторите.',
      code_expired: 'Срок действия кода истёк. Запросите новый код.'
    };

    return messages[code] || value.message || fallback;
  }

  return fallback;
}

function setAuthError(msg) {
  const el = document.getElementById('sonya-auth-error');
  if (!el) return;

  const text = normalizeAuthError(msg, 'Произошла ошибка. Попробуйте позже.');
  el.textContent = text;
  el.hidden = !text;
}
function clearAuthError() { setAuthError(''); }

/* ── consent helper (registration) ── */
function getRegisterConsents() {
  const types = ['terms_of_service', 'privacy_policy', 'personal_data_processing'];
  const out = [];
  for (const t of types) {
    const el = document.querySelector(`#sonya-auth-overlay input[name="${t}"]`);
    if (!el || !el.checked) return null;
    out.push({ type: t, version: '1.0' });
  }
  return out;
}

/* ── Step 1: request code ── */
async function handleRequestCode(purpose) {
  clearAuthError();

  const inputId = purpose === 'registration' ? 'sonya-register-email' : 'sonya-login-email';
  const btnId   = purpose === 'registration' ? 'sonya-register-code-btn' : 'sonya-login-code-btn';

  const emailEl = document.getElementById(inputId);
  const email = (emailEl?.value || '').trim().toLowerCase();
  if (!email || !email.includes('@')) { setAuthError('Введите корректный email'); return; }

  // Validate consents on registration BEFORE network call
  let consents = null;
  if (purpose === 'registration') {
    consents = getRegisterConsents();
    if (!consents) { setAuthError('Необходимо принять все три соглашения'); return; }
  }

  const btn = document.getElementById(btnId);
  setCtaLoading(btn, true);

  const res  = await apiRequestCode(email, purpose);
  const data = await safeJson(res);

  setCtaLoading(btn, false);

  if (res._networkError) { setAuthError('Сервер недоступен. Проверьте соединение и повторите попытку.'); return; }
  if (res.status === 404) { setAuthError(data.detail || 'Аккаунт не найден. Зарегистрируйтесь.'); return; }
  if (res.status === 429) { setAuthError('Слишком много попыток. Подождите и повторите.'); return; }
  if (!res.ok)            { setAuthError(data.detail || 'Ошибка отправки. Попробуйте позже.'); return; }

  _authPurpose  = purpose;
  _authEmail    = email;
  _authConsents = consents;

  setAuthView('code');

  // Update code subtitle with email
  const sub = document.getElementById('sonya-code-subtitle');
  if (sub) sub.innerHTML = `Мы отправили код подтверждения на <b>${escHtml(email)}</b>. Проверьте «Входящие» и «Спам».`;

  startResendCooldown();
  setTimeout(() => {
    const codeEl = document.getElementById('sonya-code-input');
    if (codeEl) { codeEl.value = ''; codeEl.focus(); }
  }, 80);
}

/* ── Resend code (from code view) ── */
async function handleResendCode() {
  if (Date.now() < _resendDeadline) return;
  if (!_authEmail) { setAuthView(_authPurpose === 'registration' ? 'register' : 'login'); return; }
  clearAuthError();

  const btn = document.getElementById('sonya-auth-resend');
  if (btn) { btn.disabled = true; btn.textContent = 'Отправка…'; }

  const res  = await apiRequestCode(_authEmail, _authPurpose);
  const data = await safeJson(res);

  if (btn) { btn.disabled = false; btn.textContent = 'Отправить код повторно'; }

  if (res._networkError) { setAuthError('Сервер недоступен. Проверьте соединение.'); return; }
  if (res.status === 429) { setAuthError('Превышен лимит запросов. Попробуйте позже.'); return; }
  if (!res.ok)            { setAuthError(data.detail || 'Не удалось отправить код. Попробуйте позже.'); return; }

  showToast('Код отправлен повторно', 'success');
  startResendCooldown();
}

/* ── Back from code view ── */
function goBackFromCodeView() {
  setAuthView(_authPurpose === 'registration' ? 'register' : 'login');
  clearAuthError();
  stopResendCooldown();
  setTimeout(() => {
    const id = _authPurpose === 'registration' ? 'sonya-register-email' : 'sonya-login-email';
    document.getElementById(id)?.focus();
  }, 60);
}

/* ── Step 2: verify code ── */
async function handleVerifyCode() {
  clearAuthError();
  const codeEl = document.getElementById('sonya-code-input');
  const code = (codeEl?.value || '').trim();
  if (code.length !== 6 || !/^\d{6}$/.test(code)) { setAuthError('Введите 6-значный код из письма'); return; }
  if (!_authEmail) { setAuthError('Сессия устарела. Запросите код заново.'); return; }

  const submitBtn = document.getElementById('sonya-verify-code-btn');
  setCtaLoading(submitBtn, true);

  const payload = { email: _authEmail, code, purpose: _authPurpose };
  if (_authPurpose === 'registration') {
    payload.consents = _authConsents || getRegisterConsents();
    if (!payload.consents) {
      setCtaLoading(submitBtn, false);
      setAuthError('Необходимо принять все три соглашения');
      return;
    }
  }

  const res  = await apiVerifyCode(payload);
  const data = await safeJson(res);

  setCtaLoading(submitBtn, false);

  if (res._networkError) { setAuthError('Сервер недоступен. Проверьте соединение.'); return; }
  if (res.status === 400) { setAuthError(data.detail || 'Неверный код. Попробуйте снова.'); return; }
  if (res.status === 410) { setAuthError('Срок действия кода истёк. Запросите новый код.'); return; }
  if (res.status === 429) { setAuthError('Превышено количество попыток. Запросите новый код.'); return; }
  if (!res.ok)            { setAuthError(data.detail || 'Ошибка. Попробуйте позже.'); return; }

  stopResendCooldown();
  closeAuthModal();
  await refreshAuthState();

  const user = authState.user;
  if (user) {
    showToast(data.is_new_user
      ? 'Добро пожаловать! Вам доступно 1 бесплатное видео.'
      : `С возвращением, ${user.email}!`,
      'success');
  }
}

/* ─────────────────────────────────────────────
   PAYWALL MODAL
───────────────────────────────────────────── */
function openPaywallModal() {
  document.getElementById('sonya-paywall-modal').classList.add('is-open');
}
function closePaywallModal() {
  document.getElementById('sonya-paywall-modal').classList.remove('is-open');
}

function openTelegramPayment() {
  // Stub — real flow: POST /api/billing/create-telegram-payment → open bot
  const botUsername = window.SONYA_BOT_USERNAME || 'sonyaaibot';
  const url = `https://t.me/${botUsername}`;
  window.open(url, '_blank', 'noopener');
  closePaywallModal();
  showToast('Откройте Telegram Bot SONYA для оплаты Pro Plan');
}

/* ─────────────────────────────────────────────
   GENERATE FLOW  (called from app.js)
   Returns: 'ok' | 'auth' | 'paywall' | 'error'
───────────────────────────────────────────── */
async function checkAndCreateVideoJob(formData) {
  // A. Check auth
  const meRes = await apiGetMe();
  if (meRes.status === 401 || !meRes.ok) {
    openAuthModal('login', 'Создайте аккаунт, чтобы получить 1 бесплатное видео');
    return 'auth';
  }

  // B. Try to create video job
  const jobRes  = await apiCreateVideoJob(formData);
  const jobData = await safeJson(jobRes);

  if ([200, 201, 202].includes(jobRes.status)) {
    // Refresh account state to update free_video_used counter
    window.SONYA_LAST_JOB = jobData;
    console.log('[SONYA] generation job created', jobData);
    await refreshAuthState();
    return 'ok';
  }

  if (jobRes.status === 402) {
    const code = jobData?.detail?.code;
    if (code === 'FREE_PLAN_USED') {
      openPaywallModal();
      return 'paywall';
    }
  }

  if (jobRes.status === 401) {
    openAuthModal('login', 'Создайте аккаунт, чтобы получить 1 бесплатное видео');
    return 'auth';
  }

  // Generic error
  const msg = jobData?.detail?.message || jobData?.detail || 'Ошибка создания задания';
  showToast(msg, 'error');
  return 'error';
}

/* ─────────────────────────────────────────────
   TOAST
───────────────────────────────────────────── */
function showToast(message, type = 'info') {
  let toast = document.getElementById('sonya-toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'sonya-toast';
    document.body.appendChild(toast);
  }
  const icons = { success: '\u2713', error: '\u26A0', info: '' };
  const ic = icons[type] || '';
  toast.innerHTML = ic
    ? `<span class="sonya-toast-ic" aria-hidden="true">${ic}</span><span>${escHtml(message)}</span>`
    : escHtml(message);
  toast.className = `sonya-toast sonya-toast--${type} is-visible`;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => toast.classList.remove('is-visible'), 4200);
}

/* ─────────────────────────────────────────────
   HELPERS
───────────────────────────────────────────── */
function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

async function safeJson(res) {
  try { return await res.json(); } catch { return {}; }
}

/* ─────────────────────────────────────────────
   INIT
───────────────────────────────────────────── */
function initAuth() {
  // Account button
  const accountBtn = document.getElementById('sonya-account-btn');
  if (accountBtn) {
    accountBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const drop = document.getElementById('sonya-account-drop');
      if (drop && drop.classList.contains('is-open')) closeAccountDropdown();
      else openAccountDropdown();
    });
  }

  /* ── Auth modal ── */
  // Close
  document.getElementById('sonya-auth-close')?.addEventListener('click', closeAuthModal);
  document.getElementById('sonya-auth-overlay')?.addEventListener('click', (event) => {
    if (event.target.id === 'sonya-auth-overlay') closeAuthModal();
  });

  // Tabs (login / register)
  document.querySelectorAll('[data-auth-tab]').forEach(tab => {
    tab.addEventListener('click', () => {
      setAuthView(tab.dataset.authTab);
      clearAuthError();
    });
  });

  // Inline switch buttons inside views
  document.querySelectorAll('[data-switch-auth]').forEach(btn => {
    btn.addEventListener('click', () => {
      setAuthView(btn.dataset.switchAuth);
      clearAuthError();
    });
  });

  // Login flow
  const loginEmail = document.getElementById('sonya-login-email');
  if (loginEmail) {
    loginEmail.addEventListener('input', clearAuthError);
    loginEmail.addEventListener('keydown', e => { if (e.key === 'Enter') handleRequestCode('login'); });
  }
  document.getElementById('sonya-login-code-btn')?.addEventListener('click', () => handleRequestCode('login'));

  // Register flow
  const regEmail = document.getElementById('sonya-register-email');
  if (regEmail) {
    regEmail.addEventListener('input', clearAuthError);
    regEmail.addEventListener('keydown', e => { if (e.key === 'Enter') handleRequestCode('registration'); });
  }
  document.getElementById('sonya-register-code-btn')?.addEventListener('click', () => handleRequestCode('registration'));

  // Code flow
  const codeInput = document.getElementById('sonya-code-input');
  if (codeInput) {
    codeInput.addEventListener('input', () => {
      codeInput.value = codeInput.value.replace(/\D/g, '').slice(0, 6);
      clearAuthError();
    });
    codeInput.addEventListener('keydown', e => { if (e.key === 'Enter') handleVerifyCode(); });
  }
  document.getElementById('sonya-verify-code-btn')?.addEventListener('click', handleVerifyCode);
  document.getElementById('sonya-auth-resend')?.addEventListener('click', handleResendCode);
  document.getElementById('sonya-auth-back')?.addEventListener('click', goBackFromCodeView);

  // Esc closes any open modal
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    const auth = document.getElementById('sonya-auth-overlay');
    if (auth && auth.classList.contains('is-open')) { closeAuthModal(); return; }
    const open = document.querySelector('.sonya-modal-overlay.is-open');
    if (!open) return;
    if (open.id === 'sonya-paywall-modal') closePaywallModal();
  });

  // Paywall modal
  document.getElementById('paywall-modal-close')?.addEventListener('click', closePaywallModal);
  document.getElementById('sonya-paywall-modal')?.addEventListener('click', e => {
    if (e.target === e.currentTarget) closePaywallModal();
  });
  document.getElementById('paywall-tg-btn')?.addEventListener('click', openTelegramPayment);
  document.getElementById('paywall-later-btn')?.addEventListener('click', closePaywallModal);

  // Initial state fetch (non-blocking)
  refreshAuthState();
}

// Auto-init when DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initAuth);
} else {
  initAuth();
}
