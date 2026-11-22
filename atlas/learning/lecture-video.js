// The external watch link also works in file URLs, which have no HTTP referrer.
if (location.protocol === 'http:' || location.protocol === 'https:') {
  const section = document.querySelector('.lecture-video');
  const slot = section.querySelector('.inline-video');
  const watchLink = section.querySelector('.watch-recording');
  const watchURL = new URL(watchLink.href);
  const videoId = watchURL.searchParams.get('v');
  const embedURL = 'https://www.youtube-nocookie.com/embed/' + encodeURIComponent(videoId);

  const player = document.createElement('iframe');
  player.title = slot.dataset.title;
  player.referrerPolicy = 'strict-origin-when-cross-origin';
  player.allow = 'autoplay; encrypted-media; picture-in-picture; fullscreen';
  player.allowFullscreen = true;
  const start = watchURL.searchParams.get('t');
  player.src = /^\d+s$/.test(start) ? embedURL + '?start=' + parseInt(start, 10) : embedURL;
  slot.append(player);

  for (const moment of section.querySelectorAll('.video-moments a')) {
    moment.addEventListener('click', event => {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
      const timestamp = new URL(moment.href).searchParams.get('t');
      if (!/^\d+s$/.test(timestamp)) return;
      event.preventDefault();
      player.src = embedURL + '?autoplay=1&start=' + parseInt(timestamp, 10);
      player.focus();
      player.scrollIntoView({block: 'nearest'});
    });
  }
}
