/* Scroll reveals, ~1 KB, no dependencies.

   Elements marked data-reveal start slightly offset and faded (see the CSS),
   then get .is-visible the first time they scroll into view. IntersectionObserver
   does the watching, so there is no scroll handler firing on every frame.

   Anyone who has asked their system for less motion, and anyone whose browser
   lacks IntersectionObserver, gets everything revealed immediately instead. */
(function () {
  "use strict";

  var items = document.querySelectorAll("[data-reveal]");
  if (!items.length) return;

  var reduced = window.matchMedia
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function showAll() {
    for (var i = 0; i < items.length; i++) items[i].classList.add("is-visible");
  }

  if (reduced || !("IntersectionObserver" in window)) {
    showAll();
    return;
  }

  var observer = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (!entry.isIntersecting) return;
      entry.target.classList.add("is-visible");
      observer.unobserve(entry.target);   // reveal once, then stop watching
    });
  }, {
    // Fire a little before the element is fully on screen.
    rootMargin: "0px 0px -12% 0px",
    threshold: 0.12
  });

  for (var i = 0; i < items.length; i++) observer.observe(items[i]);
})();
