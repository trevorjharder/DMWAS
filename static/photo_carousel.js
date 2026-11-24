const carousel = document.querySelector("[data-photo-carousel]");
const photoTrack = carousel.querySelector(".about-gallery__track");
const previousPhoto = carousel.querySelector("[data-photo-previous]");
const nextPhoto = carousel.querySelector("[data-photo-next]");

function updatePhotoArrows() {
  const atStart = photoTrack.scrollLeft <= 1;
  const atEnd = photoTrack.scrollWidth - photoTrack.clientWidth - photoTrack.scrollLeft <= 1;
  previousPhoto.setAttribute("aria-disabled", String(atStart));
  nextPhoto.setAttribute("aria-disabled", String(atEnd));
}

function movePhotoTrack(event, direction) {
  if (event.currentTarget.getAttribute("aria-disabled") === "true") {
    return;
  }

  const firstPhoto = photoTrack.firstElementChild;
  const cardWidth = firstPhoto.getBoundingClientRect().width;
  const gap = parseFloat(getComputedStyle(photoTrack).columnGap);
  photoTrack.scrollBy({ left: direction * (cardWidth + gap) });
}

previousPhoto.addEventListener("click", (event) => movePhotoTrack(event, -1));
nextPhoto.addEventListener("click", (event) => movePhotoTrack(event, 1));
photoTrack.addEventListener("scroll", updatePhotoArrows, { passive: true });
new ResizeObserver(updatePhotoArrows).observe(photoTrack);

previousPhoto.hidden = false;
nextPhoto.hidden = false;
updatePhotoArrows();
