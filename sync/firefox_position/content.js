/*
 * content.js - ページ内の <video> を監視して、位置を background に送ります。
 */
(function () {
  'use strict';

  let lastVideo = null;

  function findVideo() {
    const videos = Array.from(document.querySelectorAll('video'));
    return videos.length ? videos[videos.length - 1] : null;
  }

  function report() {
    const video = lastVideo && document.contains(lastVideo) ? lastVideo : findVideo();
    if (!video) {
      return;
    }
    lastVideo = video;
    const payload = {
      url: location.href,
      title: document.title,
      currentTime: video.currentTime,
      duration: Number.isFinite(video.duration) ? video.duration : 0,
      paused: video.paused,
      muted: video.muted,
      volume: video.volume,
      rate: video.playbackRate,
    };
    try {
      browser.runtime.sendMessage({ type: 'position', payload: payload });
    } catch (e) {
      /* 拡張が読み直された直後などに一時的に失敗する */
    }
  }

  browser.runtime.onMessage.addListener((message) => {
    if (message && message.type === 'ping') {
      report();
    }
  });

  setInterval(report, 500);
  report();
})();
