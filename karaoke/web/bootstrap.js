import init from './frontier_karaoke_web.js';

init().catch((error) => {
  document.body.textContent = `K歌实时媒体平面载入失败：${error}`;
});
