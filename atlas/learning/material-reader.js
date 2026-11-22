function readerButton(text, action) {
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = text;
  button.addEventListener('click', action);
  return button;
}

for (const reader of document.querySelectorAll('.material-reader[data-start]')) {
  const viewport = reader.querySelector('.material-pages');
  const pages = Array.from(viewport.children);
  const controls = document.createElement('div');
  controls.className = 'material-controls';
  controls.setAttribute('role', 'group');
  controls.setAttribute('aria-label', reader.querySelector('figcaption').textContent);
  let selected = Number(reader.dataset.start) - 1;

  if (pages.length > 1) {
    const choose = document.createElement('select');
    pages.forEach((page, index) => choose.add(new Option(String(index + 1), String(index))));
    const label = document.createElement('label');
    label.append('Страница ', choose, ' из ' + pages.length);
    const back = readerButton('← Назад', () => show(selected - 1));
    const next = readerButton('Далее →', () => show(selected + 1));
    controls.append(back, label, next);
    choose.addEventListener('change', () => show(Number(choose.value)));
    function show(index) {
      selected = index;
      pages.forEach((page, n) => { page.hidden = n !== selected; });
      choose.value = String(index);
      back.disabled = index === 0;
      next.disabled = index === pages.length - 1;
      viewport.scrollTo(0, 0);
    }
    show(selected);
  }
  const zoom = readerButton('Увеличить', () => {
    const enlarged = viewport.classList.toggle('enlarged');
    zoom.textContent = enlarged ? 'По ширине' : 'Увеличить';
    zoom.setAttribute('aria-pressed', String(enlarged));
    viewport.scrollTo(0, 0);
  });
  zoom.setAttribute('aria-pressed', 'false');
  controls.append(zoom);
  viewport.before(controls);
}

for (const link of document.querySelectorAll('a.source-page[data-preview]')) {
  const figure = link.nextElementSibling;
  if (!figure || !figure.matches('.source-image')) continue;
  const anchor = figure.querySelector('a');
  const image = anchor.querySelector('img');
  const crop = {src: image.src, alt: image.alt, width: image.getAttribute('width'),
                height: image.getAttribute('height'), label: link.textContent, href: anchor.href};
  link.setAttribute('aria-expanded', 'false');
  link.addEventListener('click', event => {
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    const full = link.getAttribute('aria-expanded') === 'false';
    link.setAttribute('aria-expanded', String(full));
    link.textContent = full ? 'Вернуться к фрагменту' : crop.label;
    image.src = full ? link.dataset.preview : crop.src;
    image.alt = full ? crop.label + '. ' + crop.alt : crop.alt;
    anchor.href = full ? link.dataset.preview : crop.href;
    if (full) {
      image.removeAttribute('width');
      image.removeAttribute('height');
    } else {
      image.setAttribute('width', crop.width);
      image.setAttribute('height', crop.height);
    }
  });
}
