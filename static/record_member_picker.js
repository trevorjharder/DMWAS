function normalizeMemberSearch(value) {
  return value.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase().trim();
}

document.querySelectorAll("[data-member-picker]").forEach((picker) => {
  const form = picker.closest("form");
  const searchControls = picker.querySelector("[data-member-search-controls]");
  const search = searchControls.querySelector("input");
  const status = searchControls.querySelector("[role='status']");
  const options = picker.querySelector("[data-member-options]");
  const members = Array.from(options.children, (row) => ({
    row,
    checkbox: row.querySelector("input"),
    searchText: normalizeMemberSearch(row.textContent),
  }));

  function updateStatus() {
    const selected = members.filter(({ checkbox }) => checkbox.checked).length;
    const message = options.hidden ? "No members match your search. " : "";
    status.textContent = `${message}${selected} selected`;
  }

  function filterMembers() {
    const terms = normalizeMemberSearch(search.value).split(/\s+/).filter(Boolean);
    members.forEach(({ row, searchText }) => {
      row.hidden = !terms.every((term) => searchText.includes(term));
    });
    options.hidden = members.every(({ row }) => row.hidden);
    options.scrollTop = 0;
    updateStatus();
  }

  search.addEventListener("input", () => {
    search.setCustomValidity("");
    filterMembers();
  });
  search.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.isComposing) event.preventDefault();
  });
  options.addEventListener("change", () => {
    search.setCustomValidity("");
    updateStatus();
  });
  form.addEventListener("submit", (event) => {
    if (members.some(({ checkbox }) => checkbox.checked)) return;

    event.preventDefault();
    search.setCustomValidity("Select at least one member who took part.");
    search.reportValidity();
  });

  filterMembers();
  searchControls.hidden = false;
});
