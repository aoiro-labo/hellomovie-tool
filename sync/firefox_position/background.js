/*
 * background.js - content から受け取った再生位置を localhost に流します。
 *
 * 送信先: http://127.0.0.1:8765/position  (hellomovie_sync.py の位置受信サーバ)
 * 映��が 1 つもないときは「どのタブの動画か」を(web ページ側の情報として) 送ります。
 */
(function () {
  'use strict';

  const ENDPOINT = 'http://127.0.0.1:8765/position';
  let lastSent = 0;

  function send(payload) {
    const now = Date.now();
    if (now - lastSent < 400) {
      return;
    }
    lastSent = now;
    fetch(ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }).catch(() => {
      /* 受信サーバが起動していないときは黙って無視 */
    });
  }

  browser.runtime.onMessage.addListener((message) => {
    if (message && message.type === 'position') {
      send(message.payload);
    }
  });

  // タブが切り替わったときに Ping を飛ばす
  browser.tabs.onActivated.addListener(() => {
    browser.tabs.query({ active: true, currentWindow: true }).then((tabs) => {
      if (tabs.length) {
        browser.tabs.sendMessage(tabs[0].id, { type: 'ping' }).catch(() => {});
      }
    });
  });
})();
