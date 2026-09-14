(() => {
  const select = document.getElementById('theme-select');
  if (!select) return;
  const saved = localStorage.getItem('contributarr-theme') || 'auto';
  select.value = saved;
  select.addEventListener('change', () => {
    document.documentElement.dataset.theme = select.value;
    localStorage.setItem('contributarr-theme', select.value);
  });
})();
