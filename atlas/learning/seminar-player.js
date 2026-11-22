// Watch links remain usable in an offline export and without JavaScript.
if (location.protocol === 'https:' || location.protocol === 'http:') {
  let screen;
  function closeSeminar() {
    screen?.remove();
    screen = null;
  }
  document.addEventListener('atlas:lecture-start', closeSeminar);
  document.addEventListener('click', event => {
    const link = event.target.closest('a.seminar-watch');
    if (!link || event.defaultPrevented || event.button !== 0 ||
        event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const url = new URL(link.href);
    const video = url.searchParams.get('v');
    const item = link.closest('.seminar-item');
    if (!item || url.origin !== 'https://www.youtube.com' || url.pathname !== '/watch' ||
        !/^[\w-]{11}$/.test(video || '')) return;
    event.preventDefault();
    document.dispatchEvent(new Event('atlas:seminar-start'));
    closeSeminar();
    const frame = document.createElement('iframe');
    frame.title = 'Видеозапись семинара · ' + link.textContent.trim();
    const embed = new URL('https://www.youtube.com/embed/' + video);
    const time = url.searchParams.get('t');
    if (/^\d+s$/.test(time || '')) embed.searchParams.set('start', parseInt(time, 10));
    embed.searchParams.set('autoplay', '1');
    frame.src = embed.href;
    frame.allow = 'autoplay; encrypted-media; picture-in-picture; fullscreen';
    frame.allowFullscreen = true;
    frame.referrerPolicy = 'strict-origin-when-cross-origin';
    screen = document.createElement('div');
    screen.className = 'seminar-screen';
    const direct = document.createElement('a');
    direct.href = link.href;
    direct.target = '_blank';
    direct.rel = 'noopener';
    direct.textContent = 'Открыть на YouTube';
    screen.append(frame, direct);
    item.append(screen);
    screen.scrollIntoView({block:'nearest'});
  });
}
