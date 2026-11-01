/**
 * Dédalo — i18n ligero del cliente.
 *
 * Script clásico (sin inline). Expone globales:
 *   t(key, params)   -> cadena traducida (idioma actual -> inglés -> clave)
 *   getLang()        -> 'en' | 'es'
 *   setLang(code)    -> guarda en localStorage ('dedalo.lang') y recarga
 *   applyI18n(root)  -> traduce el DOM (data-i18n*, ver más abajo)
 *   window.i18nReady -> Promise resuelta al cargar en.json y el idioma activo
 *
 * Atributos soportados por applyI18n():
 *   data-i18n="k"              -> textContent
 *   data-i18n-html="k"         -> innerHTML (solo catálogos propios, de confianza)
 *   data-i18n-title="k"        -> atributo title
 *   data-i18n-placeholder="k"  -> atributo placeholder
 *   data-i18n-aria-label="k"   -> atributo aria-label
 *
 * Además, envuelve window.fetch para añadir la cabecera X-Dedalo-Lang a toda
 * petición same-origin cuyo path empiece por /api/.
 */
(function () {
  'use strict';

  var STORAGE_KEY = 'dedalo.lang';
  var DEFAULT_LANG = 'en';
  var SUPPORTED = ['en', 'es'];
  var CATALOG_BASE = '/assets/locales/';

  var origFetch = window.fetch ? window.fetch.bind(window) : null;

  function readStoredLang() {
    try {
      var v = window.localStorage.getItem(STORAGE_KEY);
      if (v && SUPPORTED.indexOf(v) !== -1) return v;
    } catch (e) { /* almacenamiento no disponible */ }
    return DEFAULT_LANG;
  }

  var currentLang = readStoredLang();
  var catalogs = { en: {}, es: {} };

  try { document.documentElement.lang = currentLang; } catch (e) { /* noop */ }

  function getLang() {
    return currentLang;
  }

  function setLang(code) {
    if (SUPPORTED.indexOf(code) === -1) code = DEFAULT_LANG;
    try { window.localStorage.setItem(STORAGE_KEY, code); } catch (e) { /* noop */ }
    window.location.reload();
  }

  function interpolate(str, params) {
    if (!params) return str;
    return str.replace(/\{(\w+)\}/g, function (m, name) {
      return Object.prototype.hasOwnProperty.call(params, name) && params[name] != null
        ? String(params[name])
        : m;
    });
  }

  function t(key, params) {
    var cur = catalogs[currentLang] || {};
    var str;
    if (Object.prototype.hasOwnProperty.call(cur, key)) str = cur[key];
    else if (Object.prototype.hasOwnProperty.call(catalogs.en, key)) str = catalogs.en[key];
    else str = key;
    return interpolate(str, params);
  }

  var ATTR_MAP = [
    ['data-i18n-title', 'title'],
    ['data-i18n-placeholder', 'placeholder'],
    ['data-i18n-aria-label', 'aria-label'],
  ];

  function applyI18n(root) {
    root = root || document;
    var scope = root.querySelectorAll ? root : document;

    function each(selector, fn) {
      if (root.matches && root !== document && root.matches(selector)) fn(root);
      scope.querySelectorAll(selector).forEach(fn);
    }

    each('[data-i18n]', function (el) {
      el.textContent = t(el.getAttribute('data-i18n'));
    });
    each('[data-i18n-html]', function (el) {
      el.innerHTML = t(el.getAttribute('data-i18n-html'));
    });
    ATTR_MAP.forEach(function (pair) {
      each('[' + pair[0] + ']', function (el) {
        el.setAttribute(pair[1], t(el.getAttribute(pair[0])));
      });
    });
  }

  function loadCatalog(code) {
    if (!origFetch) return Promise.resolve({});
    return origFetch(CATALOG_BASE + encodeURIComponent(code) + '.json')
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .catch(function () { return {}; });
  }

  // Cabecera X-Dedalo-Lang en todas las peticiones a /api/ del mismo origen
  if (origFetch) {
    window.fetch = function (input, init) {
      try {
        var urlStr = typeof input === 'string' ? input : (input && input.url) || String(input);
        var u = new URL(urlStr, window.location.href);
        if (u.origin === window.location.origin && u.pathname.indexOf('/api/') === 0) {
          var baseHeaders = (init && init.headers) ||
            (typeof Request !== 'undefined' && input instanceof Request ? input.headers : undefined);
          var headers = new Headers(baseHeaders);
          headers.set('X-Dedalo-Lang', currentLang);
          init = Object.assign({}, init, { headers: headers });
        }
      } catch (e) { /* ante la duda, petición sin modificar */ }
      return origFetch(input, init);
    };
  }

  var wanted = currentLang === DEFAULT_LANG ? [DEFAULT_LANG] : [DEFAULT_LANG, currentLang];
  window.i18nReady = Promise.all(wanted.map(loadCatalog)).then(function (list) {
    wanted.forEach(function (code, i) { catalogs[code] = list[i] || {}; });
  });

  window.t = t;
  window.getLang = getLang;
  window.setLang = setLang;
  window.applyI18n = applyI18n;
})();
