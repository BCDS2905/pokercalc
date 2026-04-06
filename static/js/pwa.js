// ── Service Worker ──
if('serviceWorker' in navigator){
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js', {scope:'/'})
      .then(r => console.log('[SW] registrado, scope:', r.scope))
      .catch(e => console.warn('[SW] registro falhou:', e));
  });
}
// ── Install Prompt (A2HS) ──
let _deferredPrompt = null;
window.addEventListener('beforeinstallprompt', e => {
  e.preventDefault();
  _deferredPrompt = e;
  const btn = document.getElementById('pwa-install-btn');
  if(btn) btn.style.display = 'flex';
});
window.addEventListener('appinstalled', () => {
  _deferredPrompt = null;
  const btn = document.getElementById('pwa-install-btn');
  if(btn) btn.style.display = 'none';
});
function installPWA(){
  if(!_deferredPrompt) return;
  _deferredPrompt.prompt();
  _deferredPrompt.userChoice.then(() => {
    _deferredPrompt = null;
    const btn = document.getElementById('pwa-install-btn');
    if(btn) btn.style.display = 'none';
  });
}
