// A plain watch link remains usable from disk and with JavaScript disabled.
if (location.protocol === 'http:' || location.protocol === 'https:') {
  const lecture = document.querySelector('.lecture-video');
  const mainSlot = lecture.querySelector('.inline-video');
  const watch = lecture.querySelector('.watch-recording');
  const watchURL = new URL(watch.href);
  const videoId = watchURL.searchParams.get('v');
  const moments = document.querySelectorAll('.video-moments a, .primary-source a');
  let activeSlot;

  function show(slot, url, play) {
    const player = document.createElement('iframe');
    player.title = slot.dataset.title;
    player.referrerPolicy = 'strict-origin-when-cross-origin';
    player.allow = 'autoplay; encrypted-media; picture-in-picture; fullscreen';
    player.allowFullscreen = true;

    const embed = new URL('https://www.youtube-nocookie.com/embed/' + encodeURIComponent(videoId));
    const time = url.searchParams.get('t');
    if (time) embed.searchParams.set('start', parseInt(time, 10));
    if (slot.dataset.end) embed.searchParams.set('end', slot.dataset.end);
    if (play) embed.searchParams.set('autoplay', '1');
    player.src = embed.href;

    if (activeSlot && activeSlot !== slot) activeSlot.replaceChildren();
    slot.replaceChildren(player);
    if (slot !== mainSlot) {
      const direct = document.createElement('a');
      direct.href = url.href;
      direct.target = '_blank';
      direct.rel = 'noopener';
      direct.textContent = 'Открыть этот момент на YouTube';
      slot.append(direct);
      const resume = document.createElement('button');
      resume.type = 'button';
      resume.className = 'watch-recording';
      resume.textContent = 'Смотреть лекцию здесь';
      resume.addEventListener('click', () => show(mainSlot, watchURL, true));
      mainSlot.replaceChildren(resume);
    }
    activeSlot = slot;
    if (play) {
      player.focus({preventScroll: true});
      player.scrollIntoView({block: 'nearest'});
    }
  }

  // A direct link to the topic opens the lecturer at that topic's timestamp.
  const initialSection = document.getElementById(location.hash.slice(1));
  const initial = initialSection?.querySelector('a.clip-start');
  if (initial) {
    show(initialSection.querySelector('.inline-video'), new URL(initial.href), false);
  } else {
    show(mainSlot, watchURL, false);
  }

  for (const moment of moments) {
    const url = new URL(moment.href);
    if (url.origin !== watchURL.origin || url.pathname !== watchURL.pathname ||
        url.searchParams.get('v') !== videoId || !/^\d+s$/.test(url.searchParams.get('t'))) continue;
    moment.addEventListener('click', event => {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
      event.preventDefault();
      const slot = moment.closest('section')?.querySelector('.inline-video') || mainSlot;
      show(slot, url, true);
    });
  }
}
