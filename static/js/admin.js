/**
 * Admin Panel JavaScript — Shared utilities
 * Session countdown, formatters, and common helpers.
 */

/**
 * Format seconds into MM:SS or HH:MM:SS display.
 */
function formatSessionTime(secs) {
  if (secs < 0) secs = 0;
  var h = Math.floor(secs / 3600);
  var m = Math.floor((secs % 3600) / 60);
  var s = secs % 60;
  var pad = function(n) { return String(n).padStart(2, '0'); };
  if (h > 0) {
    return pad(h) + ':' + pad(m) + ':' + pad(s);
  }
  return pad(m) + ':' + pad(s);
}


/**
 * Initialize and run the session countdown timer.
 * Updates the #session-info element every second.
 * Redirects to login when either timer hits zero.
 *
 * @param {number} idleRemaining - Seconds until idle timeout
 * @param {number} absoluteRemaining - Seconds until absolute session expiry
 */
function initSessionCountdown(idleRemaining, absoluteRemaining) {
  var idle = idleRemaining;
  var absolute = absoluteRemaining;
  var el = document.getElementById('session-info');
  if (!el) return;

  function render() {
    var idleStr = formatSessionTime(idle);
    var absStr = formatSessionTime(absolute);
    var idleClass = idle < 300 ? 'color:var(--yellow);font-weight:700;' : '';
    el.innerHTML =
      '<span style="' + idleClass + '">Idle: ' + idleStr + '</span>' +
      ' <span style="margin:0 6px;color:var(--border-default);">|</span> ' +
      '<span>Abs: ' + absStr + '</span>';
  }

  render();

  var timer = setInterval(function() {
    idle = Math.max(0, idle - 1);
    absolute = Math.max(0, absolute - 1);
    render();

    if (idle <= 0 || absolute <= 0) {
      clearInterval(timer);
      // Session expired - redirect to login
      el.innerHTML = '<span style="color:var(--red);font-weight:700;">SESSION EXPIRED</span>';
      setTimeout(function() {
        window.location.href = window.location.pathname.replace(/\/panel\/?$/, '/login');
      }, 1500);
    }
  }, 1000);
}

/**
 * TOTP auto-submit: When user types 6 digits in a TOTP field,
 * automatically trigger verification.
 *
 * @param {string} inputId - The ID of the TOTP input element
 * @param {function} submitFn - Function to call when 6 digits are entered
 */
function setupTotpAutoSubmit(inputId, submitFn) {
  var input = document.getElementById(inputId);
  if (!input) return;

  input.addEventListener('input', function() {
    // Strip non-digits
    this.value = this.value.replace(/\D/g, '');
    if (this.value.length === 6 && submitFn) {
      setTimeout(submitFn, 50);
    }
  });

  // Prevent paste of non-numeric
  input.addEventListener('paste', function(e) {
    var text = (e.clipboardData || window.clipboardData).getData('text');
    if (/\D/.test(text.trim())) {
      e.preventDefault();
    }
  });
}
