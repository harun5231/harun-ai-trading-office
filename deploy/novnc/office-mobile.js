/* Same-origin noVNC controls. No credentials, storage, clipboard or new transport. */
import UI from './app/ui.js';

const bar = document.createElement('nav');
bar.id = 'office-mobile-controls';
bar.setAttribute('aria-label', 'Kontrol browser server');
const hint = document.createElement('p');
hint.textContent = 'Ketuk kolom di browser server, lalu KEYBOARD. Bilah browser: tampilkan alamat situs.';
bar.append(hint);
const buttons = [];
function button(label, action, title) {
    const item = document.createElement('button');
    item.type = 'button'; item.textContent = label; item.title = title;
    item.disabled = true;
    // Keep noVNC's hidden keyboard input focused when tapping navigation keys.
    item.addEventListener('pointerdown', e => e.preventDefault());
    item.addEventListener('click', () => {
        if (UI.connected && UI.rfb) action();
    });
    buttons.push(item); bar.append(item); return item;
}
button('KEYBOARD', () => UI.toggleVirtualKeyboard(), 'Tampilkan/sembunyikan keyboard iPhone');
button('Tab', () => UI.rfb.sendKey(0xff09, 'Tab'), 'Pindah ke kolom berikutnya');
button('Enter', () => UI.rfb.sendKey(0xff0d, 'Enter'), 'Tekan Enter pada browser server');
button('⌫', () => UI.rfb.sendKey(0xff08, 'Backspace'), 'Hapus satu karakter');
button('Bilah browser', () => {
    UI.hideVirtualKeyboard();
    UI.rfb.sendKey(0xffc8, 'F11');
}, 'Keluar/masuk layar penuh Chromium untuk melihat alamat situs');
button('Pas layar', () => {
    UI.rfb.scaleViewport = true;
    UI.rfb.resizeSession = false;
}, 'Skalakan tampilan tanpa mengubah resolusi desktop server');
document.body.append(bar);
function update() {
    const connected = Boolean(UI.connected && UI.rfb);
    buttons.forEach(item => { item.disabled = !connected; });
    const input = document.getElementById('noVNC_keyboardinput');
    if (input) {
        // Native noVNC input only; never create a credential form or read its value.
        input.setAttribute('autocapitalize', 'none');
        input.setAttribute('autocorrect', 'off');
        input.setAttribute('autocomplete', 'off');
        input.setAttribute('spellcheck', 'false');
    }
    buttons[0].setAttribute('aria-pressed', String(document.activeElement === input));
}
new MutationObserver(update).observe(document.documentElement, {attributes: true, attributeFilter: ['class']});
document.addEventListener('focusin', update);
document.addEventListener('focusout', update);
update();
// Reserve the actual toolbar height, including wrapping and iPhone safe areas.
new ResizeObserver(() => {
    document.documentElement.style.setProperty('--office-toolbar-height', `${bar.getBoundingClientRect().height}px`);
}).observe(bar);
